# The vault

Where your logins live, so that XENIO can sign you in without anyone reading them.

An agent reaches a sign-in page in your own browser and asks. XENIO tells you which email it would place, and on which website, and your fingerprint is the yes. The email and password are placed into that window, with no keyboard and no mouse, and the agent carries on. XENIO knows your email. Nobody but the vault ever has the password.

## Using it

War Room, then **Vault**:

1. **Create your vault.** A window of the Mac's own asks for a secret password. It opens the vault when your fingerprint cannot. It is stored nowhere, and if you lose it and your fingerprint stops working, what is in the vault is gone. There is no reset.
2. **Add a login**, or type a website under **Sign in to a website**. If you sign in by hand in the window that opens, XENIO offers to save it, as Google does.
3. Next time, that window places the login after your fingerprint. An agent working on that website can ask for the same.

There is no button to show a password, and no command that returns one. If you need to read one, reset it on the website.

## How it is built

Everything secret happens in one native program, `native/hands/vault.swift`. XENIO's Python, its web page, and every model and agent are on the far side of a pipe that carries only a website's name and "filled" or "refused".

| Question | Answer |
|---|---|
| What encrypts a login? | AES-256-GCM, with a random 256-bit key |
| Can a login be moved to another website by editing the file? | No. Each is bound to its own id and website, so decrypting fails. Tested |
| What locks that key? | Two seals. Your fingerprint, through a key inside the Secure Enclave chip. And your secret password, stretched with PBKDF2-SHA256, 4,000,000 rounds, random salt |
| Is the password hashed? | Your secret password is stretched as a hash is, and never stored. Your logins are encrypted and not hashed, because a hash cannot be turned back into what a website asks for |
| Where is it? | `<data dir>/vault/vault.json`, readable by you alone. The password is sealed. The website, a label and the email or username are readable, because you asked for XENIO to know which email it would use and to ask you before placing it |
| Where do I type? | Into a window of the Mac's own. Never into XENIO's page, so it never passes through the web page, the API or a log |

## What stops a login going somewhere it should not

* **The website is checked by the browser, at the moment of filling**, not by anything an agent said. It must be the saved website or a part of it: a login for `google.com` fits `accounts.google.com`, and never `google.com.evil.com`. It must be HTTPS. The check is repeated between screens, so a page that moves elsewhere mid sign-in gets nothing more.
* **Hidden fields are not filled.** Tested with a trap field.
* **The filling runs out of reach of the page's scripts**, in a separate JavaScript world. Once filled, the box cannot be turned into plain text by a "show password" button, and it reads back to an agent as "filled from the CEO's vault". Tested.
* **Agents cannot type a password themselves**, or read a password box, and text that looks like a key is refused.

## Limits, plainly

* **A real fingerprint on the real sensor has not been tested.** A try was made with a throwaway vault, while the Mac was locked and nobody was at it, and macOS cancels every request then, even from a bare test program with nothing of XENIO's in it. What was checked: a key locked to a fingerprint really refuses to work without one, and the rest of the path works with the secret password. **Try it first with a login you do not care about.** If the prompt vanishes by itself while you are at the Mac, that is a real bug, so say so.
* **When the Mac is locked nobody can sign in.** macOS cancels the request, and the agent is told that, and carries on with what does not need it.
* **Once a password is on a page, that website can read it.** That is true of every password manager. It is why the website check matters.
* **Some websites refuse embedded browsers.** Google in particular may say the browser is not secure. If so, signing in there through XENIO will not work, and nothing can be done about that from here. It has not been tried on a real account.
* **A code or captcha needs you.** `page_ask_ceo` shows you the browser, and the agent waits until you close the window.
* **Anyone who has your Mac unlocked, your fingerprint and your attention can open it**, and so can malware running as you that catches you typing the secret password. The file alone is of no use to anyone.
* Swift strings cannot be wiped from memory on demand, so a password is in the helper's memory for as long as a sign-in takes. The helper for that sign-in runs until the agent's browser closes.
