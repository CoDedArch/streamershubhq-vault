"""Hubby's guide: how to get around Hub OS and the three portals, page by page.

It is the one source for both of Hubby's modes. In AI mode the guide entries a person can use are given to
Claude, which answers from them; in offline guide mode the website searches the same entries by the words
typed. Each entry names the page it is on, so "Take me there" always opens a real page this person can open.

Who sees an entry:
- `area`: a Hub OS area (see access.AREAS). Shown to anyone who has it.
- `portal`: "designer", "clipper" or "investor", for the Designer Studio, Clipper Desk and Investor Vault.
- `only`: a narrower rule on top: founder, lead (Founder, co-founders, department heads), finance_work
  (Finance or the Founder), streamer, not_founder.

Button and page names are written exactly as they appear on screen, in **bold**. When a page or button is
renamed, update its entry here.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Entry:
    id: str
    title: str
    where: str  # breadcrumb, e.g. "People › their page › Tasks"
    path: str  # the page "Take me there" opens
    steps: tuple[str, ...]
    words: str = ""  # other words people use for this
    area: Optional[str] = None
    portal: Optional[str] = None
    only: Optional[str] = None


def E(id, title, where, path, steps, words="", area=None, portal=None, only=None) -> Entry:
    return Entry(id=id, title=title, where=where, path=path, steps=tuple(steps), words=words, area=area, portal=portal, only=only)


GUIDE: list[Entry] = [
    # You: everyone in Hub OS ------------------------------------------------------------------------------
    E("my-pay", "See my pay", "My profile and pay › My pay", "/os/me", [
        "Open **My profile and pay** in the menu, under You.",
        "Your payments are under **My pay**, newest first.",
        "To change where you're paid, click **Change** in **Where I'm paid**, then **Save**.",
    ], "salary money payment payslip paid wage momo mobile money bank account earnings", area="me", only="not_streamer"),
    E("my-tasks", "See and finish my tasks", "My profile and pay › My open tasks", "/os/me", [
        "Open **My profile and pay** in the menu.",
        "**My open tasks** lists what you've been given, with due dates.",
        "Click **Done** when a task is finished. Finishing on time counts towards your score.",
    ], "task todo to-do assigned work due overdue deadline finish complete tick", area="me"),
    E("my-goals", "Update my goal progress", "My profile and pay › My goals this month", "/os/me", [
        "Open **My profile and pay** in the menu.",
        "Under **My goals this month**, type your new total in the box next to a goal.",
        "Click **Update**. A goal shows **Met** once you reach the target.",
    ], "goal target progress kpi update total number met", area="me"),
    E("my-score", "Understand my score", "My profile and pay › My score this month", "/os/me", [
        "Open **My profile and pay** in the menu.",
        "**My score this month** shows each part: your goals, tasks delivered on time, and your lead's rating.",
        "Tasks with no due date don't count towards on time. A part with nothing in it yet is left out.",
    ], "score rating performance points percent how worked out on track watch action", area="me"),
    E("my-agreement", "Read and sign my agreement", "My profile and pay › My agreements", "/os/me", [
        "Open **My profile and pay** in the menu.",
        "Under **My agreements**, click **Read and sign** on the one waiting for you.",
        "Read it to the end, then sign. Signed agreements stay here to read again.",
    ], "agreement contract sign signature terms offer letter", area="me"),
    E("my-vesting", "See my vesting shares", "My profile and pay › My vesting", "/os/me", [
        "Open **My profile and pay** in the menu.",
        "**My vesting** shows your shares, the cliff date, and how much has vested so far.",
    ], "vesting shares equity stake cliff percent ownership", area="me"),
    E("send-idea", "Send an idea", "Suggestions", "/os/suggestions", [
        "Open **Suggestions** in the menu, under You.",
        "Click **Send an idea**, say what it fixes and who it helps, then click **Send**.",
        "It goes to the department it's about. You'll see every answer in the same place.",
    ], "idea suggestion feedback improve complaint propose suggest", area="suggestions"),
    E("idea-answer", "See what happened to my idea", "Suggestions › My ideas", "/os/suggestions", [
        "Open **Suggestions** in the menu.",
        "Pick **My ideas** in **Show**, then click an idea to see its answers and its status.",
        "If they asked you a question, reply in the same conversation.",
    ], "idea status answer reply reopened declined adopted", area="suggestions"),
    E("phone-menu", "Find the menu on a phone", "The top bar", "/os/me", [
        "On a phone, tap **Menu** at the top right.",
        "The menu lists only the pages you can open. Tap **Close** or outside it to hide it.",
    ], "phone mobile menu navigation sidebar hamburger where lost", area="me"),
    E("sign-out", "Sign out", "The menu › Sign out", "/os/me", [
        "Open the menu. **Sign out** is at the bottom.",
    ], "logout log out sign off leave", area="me"),

    # People and teams ----------------------------------------------------------------------------------------
    E("give-task", "Give someone a task", "People and teams › their page › Tasks", "/os/people", [
        "Open **People and teams** and click the person.",
        "Under **Tasks**, type the task and pick a **Due date** if it has one.",
        "Click **Add**. They get one email a few minutes later, even if you add several.",
    ], "task assign give delegate todo job work due date deadline", area="people"),
    E("set-goal", "Set a goal for someone", "People and teams › their page › Goals", "/os/people", [
        "Open **People and teams** and click the person.",
        "Under **Goals**, enter what to reach, the target and the unit, and the date.",
        "Click **Add goal**. This month's goals are half of their score.",
    ], "goal target kpi set objective number month", area="people"),
    E("tick-task", "Mark a team member's task done or reopen it", "People and teams › their page › Tasks", "/os/people", [
        "Open **People and teams** and click the person.",
        "Under **Tasks**, click **Done** on a finished task, or **Reopen** to put it back.",
    ], "task done complete reopen tick close", area="people"),
    E("rate-person", "Rate a team member's month", "People and teams › their page › Score this month", "/os/people", [
        "Open **People and teams** and click the person.",
        "Under **Score this month**, enter **Your rating (0 to 100)** and a note if you like.",
        "Click **Save rating**. The Founder isn't rated.",
    ], "rate rating review appraisal mark score lead", area="people", only="lead"),
    E("invite", "Invite a team member", "People and teams › Invite a team member", "/os/people", [
        "Open **People and teams** and click **Invite a team member**.",
        "Fill in their name, email, job title, role, department and how they're paid.",
        "Click **Send invite**. They get a 7-day link to set a password, then go through onboarding.",
    ], "invite add new member hire onboard staff employee join account", area="people", only="lead"),
    E("score-explained", "How team scores are worked out", "People and teams › How is this worked out?", "/os/people", [
        "Open **People and teams**.",
        "Click **How is this worked out?** next to a score to see its goals, on-time and rating parts.",
    ], "score worked out explain calculation percent weights", area="people"),
    E("onboarding", "See who is still onboarding", "People and teams › Onboarding pipeline", "/os/people", [
        "Open **People and teams**.",
        "**Onboarding pipeline** shows each new person and the step they're on.",
    ], "onboarding new joiner invited pending steps welcome", area="people"),
    E("step-in", "Let someone step into another desk", "People and teams › their page › Step-in access", "/os/people", [
        "Open **People and teams** and click the person.",
        "Under **Step-in access**, tick the desks they can work on, then click **Save access**.",
        "What they change there is shown to that desk's team.",
    ], "step in access permission desk cover help another department", area="people", only="founder"),

    # Command ------------------------------------------------------------------------------------------------
    E("decide", "Decide a request from the desks", "Command › Needs your decision", "/os", [
        "Open **Command**.",
        "**Needs your decision** lists requests from the desks. Approve one, or click **Decline** and give a reason.",
    ], "decision approve request pending decline", area="command"),
    E("board-report", "Export a board report", "Command › Export board report", "/os", [
        "Open **Command** and click **Export board report**.",
        "Print it or save it as a PDF from the print window.",
    ], "board report export pdf print summary investors", area="command"),
    E("open-desk", "Open a department's desk", "Command › Departments", "/os", [
        "Open **Command**.",
        "Under **Departments**, tap a department to open its desk.",
    ], "department desk team open view", area="command"),

    # Review queue -------------------------------------------------------------------------------------------
    E("review", "Review designs and applications", "Review queue", "/os/review", [
        "Open **Review queue** and pick a list.",
        "Open an item, then click **Approve**, **Ask for changes** or **Decline**.",
        "Nothing goes live until it's approved.",
    ], "review approve application designer clipper streamer design merch decline queue", area="review"),

    # Messages ------------------------------------------------------------------------------------------------
    E("answer-message", "Answer a message from the website", "Messages", "/os/inbox", [
        "Open **Messages** and pick a message.",
        "Write your answer, then click **Send reply**, or **Send and close** if it's sorted.",
        "Use **Add note** for a note only the team sees. Replies go out by email.",
    ], "message inbox email contact reply enquiry brand press applicant", area="inbox"),

    # Investor relations --------------------------------------------------------------------------------------
    E("add-investor", "Add an investor", "Investor relations › Add an investor", "/os/capital", [
        "Open **Investor relations** and click **Add an investor**.",
        "Fill in their legal name, email, phone, amount and stake.",
        "Click **Add and send invite**. They set up their Holder ID from the email.",
    ], "investor holder add invite shareholder round", area="capital"),
    E("valuation", "Set a valuation", "Investor relations › Set a valuation", "/os/capital", [
        "Open **Investor relations** and click **Set a valuation**.",
        "Enter the valuation and its basis, then click **Save valuation**. Holders see it in their vault.",
    ], "valuation value company worth price", area="capital", only="founder"),
    E("investor-report", "Write an investor report", "Investor relations › Write a report", "/os/capital", [
        "Open **Investor relations** and click **Write a report**.",
        "Pick the month and write it, then choose **Save as draft** or **Publish now**.",
    ], "report investor update monthly letter publish", area="capital"),
    E("exit-request", "Answer an exit request", "Investor relations › Exit requests", "/os/capital", [
        "Open **Investor relations**.",
        "Under **Exit requests**, click **Answer**, then **Accept** or **Decline**. Answering moves no money.",
    ], "exit sell shares cash out request investor leave", area="capital", only="founder"),
    E("confirm-investment", "Confirm an investor's payment", "Investor relations › Register", "/os/capital", [
        "Open **Investor relations**.",
        "In **Register**, click **Confirm payment** next to the holder and add the transfer reference.",
    ], "payment confirm investment transfer register paid", area="capital", only="finance_work"),

    # Agreements ----------------------------------------------------------------------------------------------
    E("new-agreement", "Create and send an agreement", "Agreements › New agreement", "/os/agreements", [
        "Open **Agreements** and click **New agreement**.",
        "Choose the person or investor and a template, then click **Create draft**.",
        "Check the terms on the draft, then send it for their signature.",
    ], "agreement contract create send draft offer sign", area="agreements"),
    E("templates", "Edit an agreement template", "Agreements › Templates", "/os/agreements", [
        "Open **Agreements** and go to **Templates**.",
        "Click **Edit** on a template, or **New template**. Placeholders fill in for each person.",
    ], "template wording contract text placeholders", area="agreements"),
    E("end-agreement", "End or withdraw an agreement", "Agreements › the agreement", "/os/agreements", [
        "Open **Agreements** and click the agreement.",
        "Click **Withdraw** for one not signed yet, or **End early** and give the reason.",
    ], "end terminate cancel withdraw agreement contract early", area="agreements"),

    # Production ----------------------------------------------------------------------------------------------
    E("plan-streams", "Plan streams", "Production › Plan streams", "/os/production", [
        "Open **Production** and click **Plan streams**.",
        "Pick the streamer, days and times, then save. They show in **The week around today**.",
    ], "schedule stream plan book live time week", area="production"),
    E("run-of-show", "Write a stream's run of show", "Production › The week around today › Run of show", "/os/production", [
        "Open **Production**. In **The week around today**, click **Run of show** on the stream.",
        "Click **Add an item** for each segment, with its time.",
        "Click **Save run of show**. During the stream, tick items as they happen.",
    ], "run of show rundown segments running order script items", area="production"),
    E("go-live", "Start or end a stream", "Production", "/os/production", [
        "Open **Production**. The next stream is at the top.",
        "Click **Go live** when it starts and **End stream** when it ends.",
    ], "go live start end stream broadcast", area="production"),
    E("mark-moment", "Mark a moment for clippers", "Production › Mark a moment", "/os/production", [
        "Open **Production** and click **Mark a moment** during or after a stream.",
        "Say what happened and pick the kind, then click **Mark it**. Clippers see it in Fresh footage.",
    ], "moment highlight clip timestamp clutch funny", area="production"),
    E("edit-queue", "Track the edit queue", "Production › Edit queue", "/os/production", [
        "Open **Production**.",
        "**Edit queue** shows each edit as **To do**, **Editing** or **In review**.",
    ], "edit queue editor video vod cut", area="production"),
    E("cancel-stream", "Cancel a stream", "Production › The week around today", "/os/production", [
        "Open **Production**. In **The week around today**, click **Cancel** on the stream.",
        "Click **Cancel the stream** to confirm, or **Keep it**.",
    ], "cancel stream call off postpone", area="production"),
    E("streamer-setup", "Set a streamer's setup and segments", "Production › Streamer setup", "/os/production", [
        "Open **Production** and find **Streamer setup**.",
        "Set their segments, moderators and chat rules, then click **Save setup**.",
    ], "producer setup segments moderators chat rules streamer", area="production"),

    # Live, events and links ----------------------------------------------------------------------------------
    E("new-post", "Post a stream, event or news", "Live, events and links › Posts", "/os/posts", [
        "Open **Live, events and links** and click **New post**.",
        "Pick the kind, add the details and picture, then click **Save and publish**.",
        "It shows on the website straight away. **Share link** gives you a link to send.",
    ], "post stream event news website publish announce live ticket", area="posts"),
    E("new-role", "Post a job", "Live, events and links › Open roles", "/os/posts", [
        "Open **Live, events and links** and go to **Open roles**.",
        "Click **New role**, upload the flyer and add the details, then publish.",
        "It goes up on the Careers page and applications come to Messages.",
    ], "job role vacancy hire careers recruit flyer", area="posts"),
    E("link-pages", "Edit a link page", "Live, events and links › Link pages", "/os/posts", [
        "Open **Live, events and links** and go to **Link pages**.",
        "Click **Add a link** or edit one, then click **Save links**. **Copy link** shares it.",
    ], "link page linktree bio links", area="posts"),
    E("tickets", "Handle event tickets", "Live, events and links › a post › Tickets", "/os/posts", [
        "Open **Live, events and links** and click **Tickets** on the event.",
        "Call the people who reserved, then click **Mark paid** once they've paid.",
    ], "ticket booking reservation event paid", area="posts"),
    E("post-numbers", "See a post's views and clicks", "Live, events and links › a post › Numbers", "/os/posts", [
        "Open **Live, events and links** and click **Numbers** on a post.",
        "You'll see views and clicks a day and where people came from.",
    ], "numbers views clicks stats analytics traffic", area="posts"),

    # Partnerships --------------------------------------------------------------------------------------------
    E("add-brand", "Add a brand to the pipeline", "Partnerships › Add a brand", "/os/partnerships", [
        "Open **Partnerships** and click **Add a brand**.",
        "Fill in the brand, sector and contact, then click **Add to Contacted**.",
    ], "brand sponsor partner lead deal add pipeline", area="partnerships"),
    E("move-deal", "Move a deal along", "Partnerships › Deal pipeline", "/os/partnerships", [
        "Open **Partnerships**.",
        "In **Deal pipeline**, tap a card to move it to the next stage and set its value.",
    ], "deal stage pipeline signed lost in talks contract", area="partnerships"),
    E("deliverable", "Track what we owe a brand", "Partnerships › Deliverables owed to brands", "/os/partnerships", [
        "Open **Partnerships**.",
        "Under **Deliverables owed to brands**, click **Add**, say what and when, then **Save**.",
    ], "deliverable owe brand obligation due", area="partnerships"),
    E("media-kit", "Send the media kit", "Partnerships › Send media kit", "/os/partnerships", [
        "Open **Partnerships** and click **Send media kit**.",
    ], "media kit deck rate card pitch", area="partnerships"),

    # Social --------------------------------------------------------------------------------------------------
    E("verify-clips", "Verify clippers' clips", "Social › Clips to verify", "/os/social", [
        "Open **Social**.",
        "Under **Clips to verify**, click **Verify**, check the views and that it's their own channel, then confirm.",
        "Views count towards their pay only after you verify. **Correct** fixes a number.",
    ], "clip clipper verify views check pay strike", area="social"),
    E("schedule-post", "Schedule a social post", "Social › Schedule a post", "/os/social", [
        "Open **Social** and click **Schedule a post**.",
        "Pick the day, kind and platform, then click **Add to calendar**. Tap it once it's out.",
    ], "schedule post calendar content plan tiktok instagram", area="social"),
    E("update-numbers", "Update platform numbers", "Social › Update numbers", "/os/social", [
        "Open **Social** and click **Update numbers**.",
        "Enter followers, subscribers and average viewers, then click **Save**.",
    ], "followers subscribers numbers stats growth update", area="social"),

    # Creative ------------------------------------------------------------------------------------------------
    E("creative-request", "Add a creative request", "Creative › New request", "/os/creative", [
        "Open **Creative** and click **New request**.",
        "Say what's needed, for whom and by when, then click **Add to board**.",
    ], "design request poster thumbnail flyer graphic photo video creative brief", area="creative"),
    E("check-design", "Check a merch design", "Creative › Merch designs waiting for your quality check", "/os/creative", [
        "Open **Creative**.",
        "Under **Merch designs waiting for your quality check**, click **Pass** or **Ask for changes**.",
    ], "merch design check quality approve designer", area="creative"),
    E("upload-inhouse", "Upload an in-house design", "Creative › Upload an in-house design", "/os/creative", [
        "Open **Creative** and click **Upload an in-house design**.",
    ], "upload design merch in-house", area="creative"),

    # Talent (a streamer's own desk) --------------------------------------------------------------------------
    E("running-late", "Tell my producer I'm late or can't stream", "Talent › Late or can't make it", "/os/talent", [
        "Open **Talent**.",
        "Click **Running late** or **Can't stream**, add a note, and send it. Your producer is told at once.",
    ], "late cant stream sick cancel delay producer tell", area="talent", only="streamer"),
    E("stream-title", "Get my stream's title and thumbnail", "Talent › Title and thumbnail", "/os/talent", [
        "Open **Talent**.",
        "Under **Title and thumbnail**, click **Copy title**, **Copy description** and **Download thumbnail**.",
        "Set them on the platform an hour before you go live.",
    ], "title thumbnail description stream setup", area="talent", only="streamer"),
    E("streamer-pay", "See my pay as a streamer", "Talent › My pay", "/os/talent", [
        "Open **Talent**.",
        "**My pay** shows platform income this month so far and your next salary tier.",
    ], "pay salary money tier income earnings", area="talent", only="streamer"),
    E("report-gear", "Report a problem with my equipment", "Talent › My equipment", "/os/talent", [
        "Open **Talent**.",
        "Under **My equipment**, click **Report a problem** on the item, describe it, then **Send to Tech**.",
    ], "equipment gear broken mic camera pc problem fault tech", area="talent", only="streamer"),
    E("merch-signoff", "Approve merch with my name", "Talent › My merch line", "/os/talent", [
        "Open **Talent**.",
        "Under **My merch line**, click **Review**, then **I approve** or **Not this one** with a note.",
    ], "merch design approve sign off name", area="talent", only="streamer"),
    E("channel-earnings", "Send my channel earnings", "Talent › My channel earnings", "/os/talent", [
        "Open **Talent**.",
        "Under **My channel earnings**, click **Send a report** and enter the month's figures.",
    ], "channel earnings report youtube twitch income figures", area="talent", only="streamer"),
    E("brand-work", "See brand work I owe", "Talent › Brand work", "/os/talent", [
        "Open **Talent**.",
        "**Brand work** lists what you owe partners and when.",
    ], "brand sponsor deliverable owe partner", area="talent", only="streamer"),

    E("my-links", "Change the links on my profile", "Talent › My links", "/os/talent", [
        "Open **Talent**.",
        "Under **My links**, click **Add a link** or edit one, then **Save links**.",
    ], "profile links socials instagram tiktok website bio", area="talent", only="streamer"),

    # Tech and Dev ----------------------------------------------------------------------------------------------
    E("log-incident", "Log an incident", "Tech and Dev › Log an incident", "/os/tech", [
        "Open **Tech and Dev** and click **Log an incident**.",
        "Say what happened and how long the stream was down, then click **Log it**.",
        "Close it with what fixed it under **Incidents**.",
    ], "incident outage drop down internet power problem", area="tech"),
    E("add-equipment", "Add equipment to the register", "Tech and Dev › Add equipment", "/os/tech", [
        "Open **Tech and Dev** and click **Add equipment**.",
        "Fill in the item, who uses it and its condition, then click **Save**.",
    ], "equipment gear register inventory add asset", area="tech"),
    E("fix-reported", "Fix a problem a streamer reported", "Tech and Dev › Reported by streamers", "/os/tech", [
        "Open **Tech and Dev**.",
        "Under **Reported by streamers**, click **Fix**, say what you did, then **Mark fixed**.",
    ], "reported problem fix repair streamer gear", area="tech"),

    # Finance and payroll ---------------------------------------------------------------------------------------
    E("record-money", "Record income or spend", "Finance and payroll › Record income or spend", "/os/finance", [
        "Open **Finance and payroll** and click **Record income or spend**.",
        "Pick what to record and the category, add the amount and date, then click **Save**.",
        "Store sales, payouts and paid invoices are recorded by themselves.",
    ], "record income spend expense money ledger cash bank", area="finance", only="finance_work"),
    E("purchase", "Record a purchase", "Finance and payroll › Purchases", "/os/finance", [
        "Open **Finance and payroll**. Under **Purchases**, click **Record a purchase**.",
        "Add each item (and its serial number if it's gear) with **Add another item**, then save.",
    ], "purchase buy bought receipt gear equipment expense mic microphone camera laptop light", area="finance", only="finance_work"),
    E("payout-run", "Prepare the monthly payout run", "Finance and payroll › Payout run", "/os/finance", [
        "Open **Finance and payroll**. The run drafts itself on the 25th, or click **Draft this month's run**.",
        "Check each payee, then click **Send payout run for approval**.",
        "Once the Founder approves, pay each one and click **Mark paid** with the reference.",
    ], "payout payroll run salaries pay staff designers clippers month", area="finance", only="finance_work"),
    E("approve-run", "Approve the payout run", "Finance and payroll › Payout run", "/os/finance", [
        "Open **Finance and payroll**.",
        "Check the run, then click **Approve run**, or **Send back** and say what needs to change.",
    ], "approve payout run payroll sign off", area="finance", only="founder"),
    E("export-ledger", "Export the ledger", "Finance and payroll › Export ledger", "/os/finance", [
        "Open **Finance and payroll** and click **Export ledger**.",
    ], "export ledger csv spreadsheet accounts books", area="finance"),

    # Store operations -----------------------------------------------------------------------------------------
    E("orders", "Pack and ship an order", "Store operations › Orders", "/os/store", [
        "Open **Store operations**. Under **Orders**, click **Open** on the order.",
        "Move it to packing and shipped, and add the courier reference if there is one.",
    ], "order ship pack deliver courier customer", area="store"),
    E("refund", "Handle a refund request", "Store operations › Orders › Returns", "/os/store", [
        "Open **Store operations** and filter **Orders** to **Returns**.",
        "Open the order and click **Refund**, or **Refuse refund** and say why.",
    ], "refund return money back customer", area="store"),
    E("restock", "Restock a product", "Store operations › Stock alerts", "/os/store", [
        "Open **Store operations**.",
        "Under **Stock alerts**, click **Reorder**. When it arrives, click **Add stock** with the units received.",
    ], "stock restock inventory reorder units", area="store"),
    E("add-product", "Add a product", "Store operations › Add a product", "/os/store", [
        "Open **Store operations** and click **Add a product**.",
        "Add the photos and details, then click **Publish**.",
    ], "product merch add new item shop listing", area="store"),
    E("store-interest", "Follow up people who want to buy", "Store operations › Interest to follow up", "/os/store", [
        "Open **Store operations**.",
        "Under **Interest to follow up**, call or **WhatsApp** them, then **Mark contacted** or **Paid and sorted**.",
    ], "interest buyer call follow up whatsapp customer", area="store"),

    # Settings --------------------------------------------------------------------------------------------------
    E("settings", "Change a business rule", "Settings", "/os/settings", [
        "Open **Settings** in the menu, under You.",
        "Change the rule in its card, then click **Save**. Every change is recorded.",
    ], "settings rules commission clipper pay payroll store weights tiers", area="settings"),
    E("hubby-settings", "Turn Hubby's AI answers on or off", "Settings › Hubby", "/os/settings", [
        "Open **Settings** and find the **Hubby** card.",
        "Tick or untick **AI answers**, set the monthly spending cap, then click **Save**.",
    ], "hubby ai assistant bot on off cap budget key", area="settings"),

    # Suggestions, for the desks that answer ideas ------------------------------------------------------------
    E("answer-idea", "Answer an idea for my department", "Suggestions › Answer them", "/os/suggestions", [
        "Open **Suggestions** and pick an idea for your department.",
        "Reply in the conversation, or click **Recommend what to do** for the Founder to decide.",
        "You can also **Move to another department**, **Merge with a similar idea** or **Close as Not now**.",
    ], "idea answer reply recommend department suggestion", area="suggestions", only="lead"),

    # Designer Studio --------------------------------------------------------------------------------------------
    E("d-upload", "Upload a design", "Designer Studio › Upload a design", "/studio", [
        "In the Designer Studio, click **Upload a design**.",
        "Add the artwork and details, then send it. It goes to the creative check, then final approval.",
    ], "upload design artwork submit merch new", portal="designer"),
    E("d-status", "See where my design is", "Designer Studio › My designs", "/studio", [
        "Open **My designs**. Each design shows **Uploaded**, **Creative check**, **Final approval** or **Live**.",
        "If they asked for changes, click **Upload a new version**.",
    ], "design status approval live review changes version", portal="designer"),
    E("d-pay", "See my earnings and payouts", "Designer Studio › Payouts", "/studio", [
        "Your earnings this month and **Ready to pay** are at the top.",
        "**Payouts** lists what was paid. You earn when an order is paid and passes the refund window.",
        "To change where you're paid, click **Change** in **Where I'm paid**.",
    ], "earnings commission money pay payout paid momo bank", portal="designer"),
    E("d-brief", "Design for a brief", "Designer Studio › From the creative team", "/studio", [
        "Under **From the creative team**, click **Read brief**.",
        "Click **Upload a design for it** before it closes.",
    ], "brief request creative team open call", portal="designer"),
    E("d-idea", "Send the team an idea", "Designer Studio › Ideas", "/studio", [
        "Find **Ideas** and click **Suggest an idea**, then **Send**.",
    ], "idea suggestion feedback question help contact", portal="designer"),

    # Clipper Desk ----------------------------------------------------------------------------------------------
    E("c-submit", "Submit a clip", "Clipper Desk › Submit a clip", "/desk", [
        "On the Clipper Desk, click **Submit a clip**.",
        "Paste the link to the post, pick the platform and the stream it's from, then click **Submit**.",
        "It's checked by the Social team before the views count.",
    ], "submit clip link post upload tiktok youtube shorts", portal="clipper"),
    E("c-footage", "Find footage to clip", "Clipper Desk › Fresh footage", "/desk", [
        "Open **Fresh footage**, then **Open** on a stream to see its marked moments.",
        "After you post a clip of a moment, click **I posted this**.",
        "The **Logo pack and end card** are in **Download kit**.",
    ], "footage moments stream timestamps vod clip download kit logo", portal="clipper"),
    E("c-pay", "See how I get paid", "Clipper Desk › How you get paid", "/desk", [
        "**How you get paid** shows verified views, what you've earned and what's waiting to be checked.",
        "To change where you're paid, click **Change** in **Where I'm paid**.",
    ], "pay paid earnings views money rate momo", portal="clipper"),
    E("c-rules", "House rules and strikes", "Clipper Desk › House rules", "/desk", [
        "Read **House rules** on the desk.",
        "Your strikes are at the top. Your desk is paused after too many strikes.",
    ], "rules strikes paused banned flagged", portal="clipper"),
    E("c-idea", "Send the team an idea", "Clipper Desk › Ideas", "/desk", [
        "Find **Ideas** and click **Suggest an idea**, then **Send**.",
    ], "idea suggestion feedback question help contact", portal="clipper"),

    # Investor Vault --------------------------------------------------------------------------------------------
    E("i-stake", "See my stake", "Investor Vault › My stake", "/vault/home", [
        "Open **My stake** at the top of the vault.",
        "It shows what you invested, your stake and its estimated value. It's an estimate, not a market price.",
    ], "stake shares value worth invested percent", portal="investor"),
    E("i-exit", "Ask to exit", "Investor Vault › What would I get?", "/vault/home", [
        "Under **What would I get?**, compare **If I hold** and **If I exit today**.",
        "Click **Start an exit request**, then **Send the request**. You'll confirm with a one-time code.",
    ], "exit sell cash out leave withdraw shares", portal="investor"),
    E("i-reports", "Read the reports", "Investor Vault › Reports", "/vault/home", [
        "Open **Reports** and click **Read** on a report. New ones are marked **New**.",
    ], "report update monthly news letter", portal="investor"),
    E("i-docs", "Download my documents", "Investor Vault › Documents", "/vault/home", [
        "Open **Documents** and click **Download**. You'll confirm with a one-time code.",
    ], "documents download agreement certificate signed copy pdf", portal="investor"),
    E("i-message", "Message the founders", "Investor Vault › Messages", "/vault/home", [
        "Open **Messages**, write your question and click **Send**.",
        "The founders usually reply within 2 working days.",
    ], "message question founders contact ask help", portal="investor"),
    E("i-lock", "Lock the vault", "Investor Vault › Lock vault", "/vault/home", [
        "Click **Lock vault** at the top. It also locks by itself after a while away.",
    ], "lock sign out logout leave close", portal="investor"),
    E("i-holder-id", "Lost my Holder ID", "Investor Vault sign-in page", "/vault", [
        "On the vault sign-in page, click **Lost your Holder ID?** then **Ask for a new one**.",
        "The founders check it's you, cancel the old one and issue a new one.",
    ], "holder id lost forgot new reissue", portal="investor"),
]

# A few questions to start with, by page
STARTERS: dict[str, list[str]] = {
    "/os": ["How do I export a board report?", "Where do I decide requests?"],
    "/os/people": ["How do I give someone a task?", "How do I invite a team member?"],
    "/os/me": ["Where do I see my pay?", "How is my score worked out?"],
    "/os/production": ["How do I write a run of show?", "How do I mark a moment?"],
    "/os/posts": ["How do I post a stream?", "How do I post a job?"],
    "/os/social": ["How do I verify a clip?", "How do I schedule a post?"],
    "/os/creative": ["How do I add a creative request?", "How do I check a merch design?"],
    "/os/talent": ["How do I tell my producer I'm late?", "Where's my stream title?"],
    "/os/tech": ["How do I log an incident?", "How do I add equipment?"],
    "/os/finance": ["How do I record a purchase?", "How does the payout run work?"],
    "/os/store": ["How do I handle a refund?", "How do I restock a product?"],
    "/os/partnerships": ["How do I add a brand?", "How do I move a deal along?"],
    "/os/capital": ["How do I add an investor?", "How do I write a report?"],
    "/os/agreements": ["How do I send an agreement?", "How do I edit a template?"],
    "/os/suggestions": ["How do I send an idea?", "What happened to my idea?"],
    "/os/settings": ["How do I turn Hubby's AI on or off?"],
    "/studio": ["How do I upload a design?", "When do I get paid?"],
    "/desk": ["How do I submit a clip?", "Where's footage to clip?"],
    "/vault/home": ["How do I download my documents?", "How do I ask to exit?"],
}
DEFAULT_STARTERS = ["Where are my tasks?", "How do I send an idea?"]

# Who looks after each desk, for questions about a page this person can't open: area -> department
AREA_DEPARTMENT = {
    "finance": "finance", "store": "finance", "partnerships": "marketing", "social": "marketing", "posts": "marketing",
    "creative": "creative", "production": "production", "tech": "tech", "talent": "talent", "capital": "strategy",
}
