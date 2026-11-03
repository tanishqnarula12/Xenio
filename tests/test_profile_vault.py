"""The profile vault: what is kept, how it is protected, what is refused, what deleting means."""

import stat
import subprocess
import sys

import pytest

from app.personal import vault as v
from app.personal.vault import KeychainKey, ProfileVault, SecretRefused, VaultCorrupt, VaultError, VaultUnavailable
from app.safety import policy as p

needs_mac = pytest.mark.skipif(sys.platform != "darwin", reason="CommonCrypto is part of macOS")


class Key:
    def __init__(self, key=None):
        self.key = key

    def get(self):
        return self.key

    def create(self):
        self.key = b"s" * 32
        return self.key


@pytest.fixture
def vault(tmp_path):
    return ProfileVault(tmp_path / "profile_vault.db", Key())


@needs_mac
class TestStored:
    def test_a_detail_comes_back_with_where_it_came_from(self, vault):
        made = vault.add("identity", "Email", "asha@example.com", field="email")
        entry = vault.get(made["id"])
        assert entry["value"] == "asha@example.com" and entry["label"] == "Email"
        assert made["source"] == "mission_control" and made["source_words"] == "You added this in Mission Control"
        assert "value" not in made  # what is shown by default has no value

    def test_nothing_readable_on_disk_not_even_the_names(self, vault):
        vault.add("identity", "Mother tongue", "Marathi-secret-word")
        raw = vault.path.read_bytes()
        assert b"Marathi-secret-word" not in raw and b"Mother tongue" not in raw

    def test_the_file_is_only_for_this_user(self, vault):
        vault.add("identity", "City", "Pune")
        assert stat.S_IMODE(vault.path.stat().st_mode) == 0o600

    def test_a_changed_record_is_not_believed(self, vault):
        import sqlite3

        made = vault.add("identity", "City", "Pune")
        conn = sqlite3.connect(vault.path)
        blob = bytearray(conn.execute("SELECT blob FROM entries").fetchone()[0])
        blob[40] ^= 1
        conn.execute("UPDATE entries SET blob = ?", (bytes(blob),))
        conn.commit()
        conn.close()
        with pytest.raises(VaultCorrupt):
            vault.get(made["id"])

    def test_a_record_cannot_be_moved_to_another_entry(self, vault):
        import sqlite3

        a, b = vault.add("identity", "City", "Pune"), vault.add("identity", "State", "Maharashtra")
        conn = sqlite3.connect(vault.path)
        blob = conn.execute("SELECT blob FROM entries WHERE id = ?", (a["id"],)).fetchone()[0]
        conn.execute("UPDATE entries SET blob = ? WHERE id = ?", (blob, b["id"]))
        conn.commit()
        conn.close()
        with pytest.raises(VaultCorrupt):
            vault.get(b["id"])

    def test_records_without_their_key_are_not_an_empty_profile(self, vault):
        vault.add("identity", "City", "Pune")
        vault.keys.key = None
        with pytest.raises(VaultUnavailable):
            vault.entries()
        assert vault.status()["available"] is False

    def test_the_wrong_key_reads_nothing(self, vault):
        vault.add("identity", "City", "Pune")
        vault.keys.key = b"x" * 32
        with pytest.raises(VaultCorrupt):
            vault.entries()

    def test_correcting_keeps_the_origin_and_says_it_was_corrected(self, vault):
        made = vault.add("identity", "City", "Pune", source="chat")
        fixed = vault.update(made["id"], value="Mumbai")
        assert vault.get(made["id"])["value"] == "Mumbai"
        assert fixed["corrected"] is True and fixed["source"] == "chat"

    def test_deleted_is_gone(self, vault):
        made = vault.add("identity", "City", "Pune")
        assert vault.delete(made["id"]) is True
        assert vault.get(made["id"]) is None and vault.entries() == []
        assert vault.delete(made["id"]) is False

    def test_a_correction_of_a_deleted_detail_says_so(self, vault):
        made = vault.add("identity", "City", "Pune")
        vault.delete(made["id"])
        with pytest.raises(VaultError, match="not in the profile"):
            vault.update(made["id"], value="Delhi")


@needs_mac
class TestRefused:
    @pytest.mark.parametrize("label", ["Password", "Gmail password", "UPI PIN", "OTP", "CVV", "Card number", "Credit card", "API key",
                                       "Auth token", "Recovery code", "Bank account number", "IFSC", "Security answer",
                                       "Mother's maiden name", "SSN"])
    def test_secret_names(self, vault, label):
        with pytest.raises(SecretRefused):
            vault.add("other", label, "something")
        assert vault.entries() == []

    @pytest.mark.parametrize("value", ["sk-or-v1-abcdefghijklmnop0123", "4111 1111 1111 1111", "ghp_abcdefghijklmnopqrstuvwxyz0123"])
    def test_secret_values_under_any_name(self, vault, value):
        with pytest.raises(SecretRefused):
            vault.add("other", "Nickname", value)

    def test_renaming_cannot_get_around_it(self, vault):
        made = vault.add("other", "Nickname", "tiger")
        with pytest.raises(SecretRefused):
            vault.update(made["id"], label="Password")
        assert vault.get(made["id"])["label"] == "Nickname"

    @pytest.mark.parametrize("label, value", [("PIN code", "411001"), ("Pincode", "411001"), ("Mobile", "+91 98765 43210"),
                                              ("Swift experience", "3 years"), ("Passport number", "A1234567")])
    def test_ordinary_details_are_not_refused(self, vault, label, value):
        assert vault.add("other", label, value)["label"] == label

    def test_identity_documents_are_sensitive_by_themselves(self, vault):
        assert vault.add("other", "Passport number", "A1234567")["sensitive"] is True
        assert vault.add("identity", "City", "Pune")["sensitive"] is False

    def test_a_sensitive_value_is_only_ever_masked_in_a_listing(self, vault):
        vault.add("other", "Aadhaar number", "1234 5678 9012")
        listed = vault.entries(with_values=True)[0]
        assert "5678" not in listed["value"] and listed["masked"] is True

    def test_a_name_and_value_are_required(self, vault):
        with pytest.raises(VaultError):
            vault.add("identity", "", "x")
        with pytest.raises(VaultError):
            vault.add("identity", "City", "  ")
        with pytest.raises(VaultError):
            vault.add("nonsense", "City", "Pune")


class TestKeychain:
    def run(self, *results):
        calls, queue = [], list(results)

        def runner(argv, **kw):
            calls.append((argv, kw))
            return queue.pop(0)
        return runner, calls

    @pytest.mark.skipif(sys.platform != "darwin", reason="macOS Keychain")
    def test_the_new_key_goes_through_stdin_never_the_arguments(self):
        done = subprocess.CompletedProcess
        made = {}

        def runner(argv, **kw):
            if argv[1] == "-i":
                made["key"] = kw["input"].split("-w ")[1].strip()
                made["argv"] = argv
                return done(argv, 0, "", "")
            return done(argv, 0, made["key"] + "\n", "")
        key = KeychainKey(runner=runner).create()
        assert len(key) == 32 and key.hex() == made["key"] and key.hex() not in " ".join(made["argv"])

    @pytest.mark.skipif(sys.platform != "darwin", reason="macOS Keychain")
    def test_no_key_yet_is_not_an_error(self):
        runner, _ = self.run(subprocess.CompletedProcess([], 44, "", ""))
        assert KeychainKey(runner=runner).get() is None

    @pytest.mark.skipif(sys.platform != "darwin", reason="macOS Keychain")
    def test_a_locked_keychain_is_said_plainly(self):
        runner, _ = self.run(subprocess.CompletedProcess([], 51, "", ""))
        with pytest.raises(VaultUnavailable, match="locked"):
            KeychainKey(runner=runner).get()

    @pytest.mark.skipif(sys.platform != "darwin", reason="macOS Keychain")
    def test_a_keychain_that_does_not_answer_is_said_plainly(self):
        def runner(argv, **kw):
            raise subprocess.TimeoutExpired(argv, 20)
        with pytest.raises(VaultUnavailable):
            KeychainKey(runner=runner).get()


class TestItsFileAndKeyAreOutOfReach:
    def test_xenios_file_tools_cannot_read_the_vault(self, tmp_path):
        paths = p.default_path_policy()
        assert paths.check(str(tmp_path / "profile_vault.db"), "read", str(tmp_path)).blocked

    @pytest.mark.parametrize("command", ['security find-generic-password -s "XENIO Profile Vault" -a profile-key -w',
                                         "security dump-keychain -d", "security find-internet-password -s example.com -w",
                                         "security delete-generic-password -s 'XENIO Profile Vault'"])
    def test_the_terminal_cannot_read_the_key(self, command):
        assert p.classify_command(command, p.default_path_policy(), "/tmp").blocked

    def test_saving_in_ordinary_memory_is_still_refused_for_secrets(self):
        assert p.classify_memory_write("memory", {"action": "add", "content": "my password is hunter2hunter2"}).blocked


def test_mask_keeps_too_little_to_use():
    assert v.mask("A1234567") == "••••••67" and v.mask("abc") == "••••••"
