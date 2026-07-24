---
id: BS-DELEGATION-INDEPENDENT-REVIEW-AND-ACCEPTANCE
layer: delegation
risk_class: critical
default_coverage_profiles: [delegated-work, product-delivery, multi-agent-fleet]
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

**R1.** A reviewer is independent only if all four tests in `GOVERNANCE.md` §3a hold: it did not
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

### Verification diversity

**R14.** For decisions that are critical **and** depend on judgement rather than a checkable fact,
review SHOULD come from a differently-constituted reviewer — a different person, a different tool, or
a different model. Two instances of the same reasoning process are one reviewer, not two.

**R15.** This is a recommendation scaled by risk, not a universal requirement. Requiring it
everywhere makes routine work expensive and trains people to route around it.

**R16.** Where multiple reviewers are used, their **basis for agreeing** matters more than the count.
Three reviewers given the same summary have checked the summary.

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

**Stop** when the required acceptance path is unavailable — no independent reviewer exists, or the
only candidate fails a test. The correct responses are: wait, find another reviewer, or reduce the
scope until it fits a lower risk class honestly. Proceeding with a review you know is not independent
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
