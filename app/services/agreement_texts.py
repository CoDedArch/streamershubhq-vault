"""The built-in agreement templates. They are created once in every database and then belong to the Founder, who can
edit, duplicate or replace them in Hub OS → Agreements. Edits never change agreements already offered or signed.

The wording is a plain-language starting point. Have a lawyer review each template before it is used.
Placeholders in {{double braces}} are filled in for each person; see PLACEHOLDERS in AgreementService.
"""

STAFF = """## The role
{{company}} ("the label") takes on {{name}} as {{title}}, starting on {{start_date}}. You report to the person named in Hub OS and work the hours agreed with them.

## What you receive
{{terms}}

## Vesting shares
Shares named above vest monthly over the vesting period after the cliff. If you leave before the cliff, no shares vest. Shares that have vested stay yours.

## Confidentiality
Contracts, money, investor details, unreleased content and anything marked internal stay inside the label, during this agreement and after it.

## Brand and conduct
You represent the label online and on camera. No hate, harassment, gambling promotion or anything that puts the roster or our partners at risk.

## Ending this agreement
Either side can end this agreement with 30 days' written notice. The label can end it straight away for serious misconduct.

## Signed
Signed in Hub OS by {{name}} on {{signed_date}}, for {{company}} by {{founder}}."""

STREAMER_TRIAL = """## A six-month trial
This is a trial agreement between {{company}} ("the label") and {{name}}, known on stream as {{stage_name}}, for roster seat {{seat_number}}.

It runs for {{term}}, from {{start_date}} to {{end_date}}. The trial is how both sides find out whether this is the right fit before signing a full recording contract.

## What the label provides
- A studio, equipment and a stream schedule
- A producer, editors and a social team
- Brand deals pitched and negotiated for you

## What you receive
{{terms}}

## What we expect
- Stream on the agreed schedule and hit the weekly targets set with your producer
- Follow the brand and conduct rules, on stream and off
- Keep contracts, money and unreleased plans confidential

## At the end of the trial
Before {{end_date}}, the label will either offer you a full recording contract or tell you the trial is ending. Neither side owes the other anything further once the trial ends without a contract, apart from pay already earned.

## Signed
Signed in Hub OS by {{name}} on {{signed_date}}, for {{company}} by {{founder}}."""

STREAMER_CONTRACT = """## Recording contract
This contract is between {{company}} ("the label") and {{name}}, known on stream as {{stage_name}}, for roster seat {{seat_number}}. It follows a successful trial.

It runs for {{term}}, from {{start_date}} to {{end_date}}, and can be renewed by agreement before it ends.

## What the label provides
- A studio, equipment, a producer, editors and a social team
- Brand deals pitched, negotiated and managed for you
- Merch in the Streamers Hub store under your name

## What you receive
{{terms}}

## Your content and channels
Your channels stay yours. The label may use clips, footage and your name and likeness to promote you, the label and its partners while this contract runs.

## What we expect
- Stream on the agreed schedule and work towards the targets set each quarter
- Follow the brand and conduct rules, on stream and off
- Keep contracts, money and unreleased plans confidential

## Renewal and ending
Ninety days before {{end_date}}, both sides discuss renewal. Either side can end the contract early for a serious breach, after written notice and 30 days to put it right.

## Signed
Signed in Hub OS by {{name}} on {{signed_date}}, for {{company}} by {{founder}}."""

DESIGNER = """## Designer terms
These terms are between {{company}} ("the label") and {{name}} ({{handle}}) for merch designs sold in the Streamers Hub store, from {{start_date}}.

## Original work
Everything you upload is your own work. You have the right to license it, and it does not copy anyone else's.

## Approval first
Designs sell only after the Creative Lead and the Founder approve them. The label can ask for changes or decline a design.

## What you receive
{{terms}}

## Ending
Either side can end these terms at any time. Designs already live keep selling until they sell out or are taken down, and earn commission as normal.

## Signed
Signed by {{name}} on {{signed_date}}."""

CLIPPER = """## House rules
These rules are between {{company}} ("the label") and {{name}} ({{handle}}), for clips cut from label streams, from {{start_date}}.

- Tag the label and the streamer in every clip
- No misleading captions, fake giveaways or bought views
- Brand moments keep the brand name and logo visible
- Three strikes and the desk is paused

## What you receive
{{terms}}

## Ending
Either side can end this at any time. Verified views up to that day are still paid.

## Signed
Signed by {{name}} on {{signed_date}}."""

INVESTOR = """## Investment agreement
This agreement is between {{company}} ("the company") and {{name}} ("the holder").

## The investment
{{terms}}

## Reporting
The holder receives a report every month in the first year, then every quarter, in the investor vault.

## Exit
The holder can ask to exit at any time from the vault. Any buy-back follows the exit terms the company has published at the time. A value shown in the vault is an estimate, not an offer.

## Confidentiality
Reports, figures and plans shared with the holder are confidential.

## Signed
Signed in the investor vault by {{name}} on {{signed_date}}, for {{company}} by {{founder}}."""

BUILT_IN = [
    {"key": "staff-standard", "name": "Team member agreement", "audience": "staff", "stage": "standard", "body": STAFF,
     "summary": "For everyone invited into Hub OS: role, pay or vesting, confidentiality and conduct.",
     "term_months": None, "renewable": False,
     "default_terms": [{"kind": "equity", "label": "Vesting shares", "value": "1%", "detail": "Over 48 months with a 12-month cliff"}]},
    {"key": "streamer-trial", "name": "Streamer trial", "audience": "streamer", "stage": "trial", "body": STREAMER_TRIAL,
     "summary": "The first six months, counted from the day the streamer signs.",
     "term_months": 6, "renewable": False,
     "default_terms": [{"kind": "salary", "label": "Monthly pay", "value": "Salary tier", "detail": "Set on the 1st of each month from the performance score"}]},
    {"key": "streamer-contract", "name": "Streamer recording contract", "audience": "streamer", "stage": "contract", "body": STREAMER_CONTRACT,
     "summary": "The full contract after a successful trial: three to five years, renewable.",
     "term_months": 36, "min_term_months": 36, "max_term_months": 60, "renewable": True,
     "default_terms": [{"kind": "salary", "label": "Monthly pay", "value": "Salary tier", "detail": "Set on the 1st of each month from the performance score"},
                       {"kind": "revenue_share", "label": "Brand deals", "value": "Share agreed per deal", "detail": "Paid when the brand pays the label"}]},
    {"key": "designer-standard", "name": "Designer terms", "audience": "designer", "stage": "standard", "body": DESIGNER,
     "summary": "For approved merch designers.", "term_months": None, "renewable": False,
     "default_terms": [{"kind": "commission", "label": "Commission", "value": "Set when each design is approved", "detail": "On every sale, payable after the refund window"}]},
    {"key": "clipper-standard", "name": "Clipper house rules", "audience": "clipper", "stage": "standard", "body": CLIPPER,
     "summary": "For approved clippers.", "term_months": None, "renewable": False,
     "default_terms": [{"kind": "other", "label": "Pay for verified views", "value": "The clipper rate in Settings", "detail": "Per 1,000 verified views, paid monthly"}]},
    {"key": "investor-standard", "name": "Investment agreement", "audience": "investor", "stage": "standard", "body": INVESTOR,
     "summary": "Signed by each holder in the investor vault.", "term_months": None, "renewable": False, "default_terms": []},
]
