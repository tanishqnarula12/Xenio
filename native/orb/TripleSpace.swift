// Triple-press Space detection, as a pure state machine (no event tap here: see main.swift).
//
// Space #1 always goes through at once: the app gets it, nothing is delayed. Space #2, if it follows within the
// interval, is HELD: if Space #3 follows within the interval too, #2 and #3 are swallowed and XENIO is summoned (so the
// app received exactly one space); if not, #2 is released when the interval ends (a double space arrives up to one
// interval late - the only cost). Any other key, a modifier, or key repeat releases what is held first and starts over.

import Foundation

enum SpaceDecision: Equatable {
    case pass                 // deliver this event as it is
    case hold                 // keep this event (Space #2): released later or dropped on a summon
    case swallow              // drop this event
    case summon               // drop this event and summon XENIO (also drops what is held)
    case releaseHeldThenPass  // first deliver what is held, then this event
}

struct TripleSpace {
    var interval: TimeInterval = 0.45
    private(set) var count = 0
    private(set) var last: TimeInterval = 0
    private(set) var holding = false          // Space #2's key-down is held
    private(set) var heldUpPending = false    // ... and its key-up has not arrived yet
    private var swallowUps = 0                // key-ups to drop (of swallowed Space #3, and of a held #2)

    /// A Space key-down (not a repeat, no modifiers) at time `t`.
    mutating func spaceDown(at t: TimeInterval) -> SpaceDecision {
        let consecutive = count > 0 && t - last <= interval
        last = t
        if !consecutive {
            let wasHolding = holding
            reset()
            count = 1
            last = t
            return wasHolding ? .releaseHeldThenPass : .pass
        }
        if count == 1 {
            count = 2
            holding = true
            heldUpPending = true
            return .hold
        }
        // the third press: summon; Space #2 (held) and this one never reach the app
        swallowUps += heldUpPending ? 1 : 0
        swallowUps += 1
        holding = false
        heldUpPending = false
        count = 0
        return .summon
    }

    /// A Space key-up.
    mutating func spaceUp() -> SpaceDecision {
        if holding && heldUpPending {
            heldUpPending = false
            return .hold  // kept with the held key-down
        }
        if swallowUps > 0 {
            swallowUps -= 1
            return .swallow
        }
        return .pass
    }

    /// Any other key, a modifier combination or a repeat: deliver what is held first, start over.
    mutating func otherKey() -> SpaceDecision {
        let wasHolding = holding
        reset()
        return wasHolding ? .releaseHeldThenPass : .pass
    }

    /// The interval passed after Space #2 without a third press: true when the held space must be released now (the
    /// caller posts what it kept: the key-down, and the key-up if it already came; a later key-up then passes).
    mutating func timeout(at t: TimeInterval) -> Bool {
        guard holding, t - last >= interval else { return false }
        reset()
        return true
    }

    mutating func reset() {
        count = 0
        holding = false
        heldUpPending = false
    }
}

// MARK: - self-test (`XenioOrb --selftest`): run by XENIO's test suite after building

func tripleSpaceSelfTest() -> [String] {
    var failures: [String] = []
    func check(_ ok: Bool, _ what: String) { if !ok { failures.append(what) } }

    var d = TripleSpace()
    check(d.spaceDown(at: 0.00) == .pass, "first space passes")
    check(d.spaceUp() == .pass, "first space up passes")
    check(d.spaceDown(at: 0.20) == .hold, "second space is held")
    check(d.spaceUp() == .hold, "second space up is held")
    check(d.spaceDown(at: 0.40) == .summon, "third space summons")
    check(d.spaceUp() == .swallow, "third space up is swallowed")
    check(d.spaceDown(at: 2.00) == .pass, "a later space passes again")

    d = TripleSpace()
    _ = d.spaceDown(at: 0); _ = d.spaceUp()
    check(d.spaceDown(at: 0.2) == .hold, "double space: second held")
    _ = d.spaceUp()
    check(d.timeout(at: 0.5) == false, "not released before the interval")
    check(d.timeout(at: 0.7) == true, "released after the interval")
    check(d.spaceDown(at: 0.8) == .pass, "next space after a release starts over")

    d = TripleSpace()
    _ = d.spaceDown(at: 0); _ = d.spaceUp()
    check(d.spaceDown(at: 0.1) == .hold, "held before a letter")
    _ = d.spaceUp()
    check(d.otherKey() == .releaseHeldThenPass, "a letter releases the held space first")
    check(d.spaceDown(at: 0.2) == .pass, "typing 'a b c': spaces separated by letters never summon")

    d = TripleSpace()
    _ = d.spaceDown(at: 0); _ = d.spaceUp()
    check(d.spaceDown(at: 0.6) == .pass, "slow presses never summon")

    d = TripleSpace()
    _ = d.spaceDown(at: 0); _ = d.spaceUp()
    _ = d.spaceDown(at: 0.1)  // held, still down
    check(d.timeout(at: 0.6), "released while the key is still down")
    check(d.spaceUp() == .pass, "its key-up then passes")

    d = TripleSpace()
    _ = d.spaceDown(at: 0); _ = d.spaceUp()
    _ = d.spaceDown(at: 0.1)
    _ = d.spaceUp()
    check(d.otherKey() == .releaseHeldThenPass && d.spaceUp() == .pass, "nothing is swallowed after a release")
    return failures
}
