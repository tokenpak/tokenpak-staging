---
id: BS-DELEGATION-INDEPENDENT-REVIEW-AND-ACCEPTANCE
layer: delegation
risk_class: critical
default_coverage_profiles: [delegated-work, product-delivery, multi-agent]
control_points: [work.accept, integrate.shared-baseline]
---

# Independent review and acceptance

## Purpose

Ensure risk-bearing work is not accepted because the actor that produced it says it is fine.

## Applies to

Any work reviewed by anyone other than its author — which, above a risk threshold, is all of it.
**Read now.**

---

## Requirements

### Independence

**R1.** A reviewer is independent only if all four tests in `GOVERNANCE.md` section 3a hold: it did not
author the work; it does not share the author's session or take the author's assumptions as sole
input; it cannot write to the evidence it evaluates; and it does not share the author's incentive to
declare completion.

**R2.** Failing any one test means the review is not independent, whatever it is labelled. The most
common failure is the second: an agent reviewing work produced in the same session, with the same
context, reliably confirms it. It is not checking; it is agreeing with itself.

**R3.** Where a control requires `independent`, all four tests MUST hold. Where it requires
`separate-actor`, only the first must — a lighter bar for lower-risk work, and it MUST NOT be
described as independent review.

### Matching the path to the risk

**R4.** Acceptance paths are graded, and the path is chosen by risk class **before** the work
starts:

| Risk class | Acceptance path |
|---|---|
| Critical — irreversible, external, money, credentials, data | Independent review (all four tests), plus evidence retained |
| High — shared state, external visibility, hard to reverse | Independent review |
| Moderate — reversible within your control | Separate actor |
| Low — mechanical, verifiable by re-running | Automated check with a retained receipt |
| Trivial — no external effect, trivially reversible | Self-verified with a record |

**R5.** The path MUST NOT be downgraded because the work turned out to be small, or because time ran
short. Reduce scope, not assurance.

**R6.** **Trivial and low classes MUST NOT be self-assigned by the executor.** Whoever benefits from
the lighter path does not choose it. This is the loophole that swallows the table.

**R7.** Where risk class is ambiguous, the **higher** class applies until someone with authority
classifies it.

### Conducting a review

**R8.** Review is against the acceptance criteria written at intake — not against a general sense of
quality, and not against what was delivered.

**R9.** A reviewer MUST inspect evidence, not accept assertions. "The check passed" is an assertion;
the receipt is evidence.

**R10.** A reviewer MUST be able to reach "not accepted". A review that structurally cannot fail is
theatre — check that yours can, by looking at whether any has.

**R11.** Review findings MUST be recorded with their disposition: fixed, accepted as-is with reason,
or deferred with an owner and a date. A finding with no disposition is unresolved, not closed.

**R12.** Disagreement between reviewer and author MUST be resolved by someone with authority, and the
resolution recorded. It MUST NOT be resolved by whoever is more persistent.

**R13.** Dissent MUST be preserved. Where a reviewer objected and was overruled, the objection stays
in the record. It is the most useful thing in the file when the decision turns out badly.

### Staffing the lane — the operator is not a reviewer of first resort

**R25.** A lane MUST be staffed from the reviewers actually available, **including a
differently-constituted automated reviewer**. Routing a routine acceptance to the operator is not an
acceptance path. It converts the accountable human into the default blocker, which is already
forbidden (`GOVERNANCE.md` R8), and it scales worse the more work you delegate.

**R26.** An automated reviewer satisfies acceptance for any control whose authorizer is not required
to be human, provided it meets the independence tests in `GOVERNANCE.md` section 3a. Being automated
neither grants nor removes independence — a fresh session with no write access to the evidence and no
stake in declaring completion is independent; a continuation of the producer's own session is not,
however it is labelled.

**R27.** Protected actions are unaffected. An automated reviewer may accept the *result*; it never
supplies the human authorization a protected action requires. Acceptance and authorization are
separate acts (`GOVERNANCE.md` R37), and only the second is reserved to a human here.

**R28.** The operator is engaged only at a **hard stop**:

| Hard stop | Not a hard stop |
|---|---|
| An action in a protected category needs its human authorization | You would like a second opinion |
| A capability declaration bearing on a protected action is `unknown` | The change feels significant |
| A decision only the operator owns — legal, money, licence, external commitment | You are uncertain and an available reviewer could resolve it |
| No available actor can clear the blocker | Reviewing it yourself feels awkward |

**R29.** Escalating to the operator without a hard stop is **itself a defect**, recorded as one. The
cost is not the interruption; it is that a system which asks for confirmation by default trains its
operator to grant it by default, and the approval stops carrying information.

**R30.** Where an automated reviewer's verdict is used, record what reviewed, on what basis, its
verdict, and its independence position — exactly as for a human reviewer (R11). An unrecorded
automated review is indistinguishable from no review.

### Verification diversity

**R14.** For decisions that are critical **and** depend on judgement rather than a checkable fact,
review SHOULD come from a differently-constituted reviewer — a different person, a different tool, or
a different model. Two instances of the same reasoning process are one reviewer, not two.

**R15.** This is a recommendation scaled by risk, not a universal requirement. Requiring it
everywhere makes routine work expensive and trains people to route around it.

**R16.** Where multiple reviewers are used, their **basis for agreeing** matters more than the count.
Three reviewers given the same summary have checked the summary.

### When no acceptor is available

**R19.** Where the required acceptance path cannot be satisfied by any available actor — most often
because the only candidate authored the work — the correct response is to **convene a separate
acceptance lane**. Self-accepting and stalling indefinitely are both wrong, and the absence of a
reviewer is not a reason to lower the acceptance path (R5).

**R20.** An acceptance lane is: a named actor or role outside the producing context, given the
acceptance criteria and the evidence, with real authority to return *not accepted*. It may be a
person, another team, a differently-constituted agent, a scheduled governor review, or an external
reviewer.

**R21.** **Convening a lane is the producer's job; staffing it is not.** The producer may open the
lane, supply evidence, and set the deadline. They MUST NOT staff it with themselves, nor with any
actor failing one of the four independence tests in `GOVERNANCE.md` section 3a.

**R22.** A lane MUST carry a deadline and a declared behaviour on no response — escalate, or hold.
**Never auto-accept.** Silence is not acceptance, in any authority profile.

**R23.** Where no lane can be convened at all, that is a finding about capacity: record it, and the
work holds at `submitted`. Work is never accepted for want of a reviewer.

**R24.** This applies with full force when the artifact being accepted is the governance itself.
Authority to proceed and independent acceptance are separate things (`GOVERNANCE.md` R37); being
authorized to act does not make you the right party to judge your own output.

### Escalating deliberation

**R17.** Where a decision is contested and consequential, deliberation SHOULD be **advisory first**:
positions and reasoning recorded before anyone rules. Ruling first and gathering views afterwards
produces agreement, not analysis.

**R18.** Deliberation MUST have a round cap and a named decider. Unbounded deliberation is a way of
not deciding while appearing busy.

---

## Evidence and acceptance

For any accepted work, you can name: the risk class, the acceptance path required by that class, who
reviewed it, which of the four independence tests held, and what evidence they saw.

## Control points

| Control | Relevance here |
|---|---|
| `work.accept` | The independence requirement per authority profile |
| `integrate.shared-baseline` | Review before work enters what others build on |

Note that `work.accept` requires at least `separate-actor` in **every** authority profile, including
`bounded-autonomous`. Autonomy moves who reviews; it does not remove review.

## Exceptions and stop conditions

**Stop and convene a lane** (R19-R23) when the required acceptance path is unavailable — no
independent reviewer exists, or the only candidate fails a test. Waiting without opening a lane is
just a slower version of stalling, and routing it to the operator instead is not a lane (R25).

**Engage the operator only at a hard stop** (R28). If an available reviewer — human or automated —
could resolve the question, that is where it goes. Proceeding with a review you know is not independent
while recording it as one is a false record.

## Anti-patterns

- An agent reviewing its own output in the same session and confirming it.
- "Reviewed" meaning someone glanced at a summary written by the author.
- Downgrading risk class at the end to fit the review that is actually available.
- Reviews that have never once resulted in rejection.
- Findings recorded with no disposition, closed at end of quarter.
- Five reviewers, one shared summary.
- A decider ruling first, then collecting supporting opinions.

## Templates

`templates/acceptance-record.md` · `templates/decision-record.md`

## Change record

| Version | Change |
|---|---|
| 1.0.0 | Initial. |
| 1.1.0 | Added R19-R24: convene an acceptance lane when no acceptor is available. |
| 1.2.0 | Added R25-R30: staff lanes from available reviewers including automated ones; operator engaged only at a hard stop. |
