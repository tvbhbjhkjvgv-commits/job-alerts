# Job Alerts

Job alerts for several people at once. Each person (a "candidate") has their own skills, roles,
cities and years of experience, and gets their own mails. Three alerts can be switched on or off
per candidate:

| Alert | What it does | How often |
|---|---|---|
| **LinkedIn hourly** | New LinkedIn postings from the last hour in the candidate's cities, mailed when one reaches the candidate's skill match | Every hour |
| **Fast alert** | One city of the candidate's choice is searched on LinkedIn; a fitting new opening is sent at once | Every 5 minutes |
| **Career pages** | New openings read from companies' own career sites, checked against the candidate's years of experience | Every hour |

Everything runs on GitHub Actions. This repository holds the code and is public; the candidates'
settings (names, email addresses, skills) are in a second, **private** repository, `job-alerts-data`.
Job names and logs here show only a candidate's id (for example `c8f3k2ab`), never a name or an address.

## Adding a candidate (about a minute)

1. Open the settings page: `https://<owner>.github.io/job-alerts/`.
2. The first time, open **Connection**, enter the settings repository (`<owner>/job-alerts-data`) and a GitHub token, and press **Connect**. See [The token](#the-token).
3. Press **+ Add a candidate** and drop the person's resume on the page (PDF, DOCX or TXT). The name, email, skills, roles, city and years are filled in from it. The resume is read inside the browser and is not uploaded.
4. Check every field. The resume is a starting point: correct the years, remove skills the person would not claim, add the cities they want.
5. Tick the alerts the person should get, and press **Save**.

The next scans pick the new candidate up by themselves. To pause someone, untick **Active**; to remove them, **Delete this candidate**.

### Free trial and the welcome mail

A new candidate starts on a **free trial** of 7 days (the length can be changed per person).

- **Welcome mail.** When the candidate is saved, they are sent one mail saying their alerts are active, which alerts they get, and the day the trial ends. It goes out at once when the page's token may start workflows (see [The token](#the-token)); otherwise with the next hourly run. It is sent once, and only within a day of the trial starting.
- **When the trial ends** the scans skip the person by themselves; nothing more is sent. Their name shows "trial ended" in the list.
- **To keep someone on:** open them on the page and either raise the trial length, press **Start a new trial from today** (which also sends a new welcome mail), or set Access to **Full access**, which has no end date.

### What the candidate receives

Each alert mail is a title and the job tables, nothing else: match percentage, the job with its link and company, where, the years asked for, when it was posted, and which of the person's skills the posting names. The table is sized to fit a mail window, so it reads without scrolling sideways. Nothing about how or where the scans run is in any mail.

### What each setting does

| Setting | Effect |
|---|---|
| Roles | The LinkedIn search words, and the job titles that are kept. A title is kept when it has every word of one role, in any order. |
| Skills | The match percentage is the share of a posting's skills that are in this list. |
| Must-have skills | A posting that names none of these is dropped. Leave empty for no such rule. |
| Python developer rules | For Python and GenAI developers. Also drops jobs that mention Python but are really Java, .NET, Node, data-engineering or DevOps jobs. |
| Job families to leave out | For everyone else: a job whose title belongs to a ticked family (Java, .NET, QA / testing, ...) is dropped, unless the title also names a must-have skill. |
| Words that rule a title out | Any title containing one of these words is dropped. |
| Cities, remote jobs | Where the jobs may be. Remote means remote and open to people in India. |
| Years of experience | A posting is kept when its stated years include this figure ("2-4 years" for 3; not "5+"). Interns, contracts and part-time jobs are always dropped. Titles such as Staff, Principal and Architect are dropped below 8 years; Senior and Lead titles that state no years are dropped below 5. |
| Skill match needed | The lowest match percentage that is sent, per alert. |
| Mail every hour | Off: a LinkedIn mail goes out only when there is a new match. On: a status mail every hour as well. |
| Companies | The career sites read for this candidate. None ticked means all of them. |
| Phone push topic | Optional. A topic on [ntfy.sh](https://ntfy.sh); the candidate subscribes to it in the ntfy app to get a push with each alert. |

## One-time setup

Already done for this repository unless noted.

1. **Two repositories** under the same owner: this one (public) and `job-alerts-data` (private, holding `candidates/<id>.json`).
2. **Read access for the scans.** A read-only deploy key on `job-alerts-data`, with its private half stored here as the secret `DATA_REPO_KEY`.
3. **The sending mailbox (to do by the owner).** Two secrets here, under Settings -> Secrets and variables -> Actions:
   - `GMAIL_ADDRESS`: the Gmail address the alerts are sent from.
   - `GMAIL_APP_PASSWORD`: an app password for that mailbox (Google account -> Security -> 2-Step Verification -> App passwords).
   Until both exist no mail can be sent, and a run that has something to send ends in red.
4. **The hourly timer.** An environment named `hourly-timer` with a 55-minute wait timer (the timer waits there without using a runner).
5. **The settings page.** GitHub Pages serving the `docs/` folder of the `main` branch.
6. **Start the scans** (once, after step 3): enable and run "Timer Watchdog", or run "Hourly Timer" and "Fast Alert" from the Actions tab. They keep themselves going from then on.

### The token

The settings page saves each candidate as a file in `job-alerts-data`, so it needs a token that may write there. Create a fine-grained token (GitHub -> Settings -> Developer settings -> Personal access tokens -> Fine-grained tokens) with access to **only** `job-alerts-data` and the permission **Contents: read and write**. The page keeps it in the browser it was typed into; "Forget token" removes it.

For the welcome mail to go out the moment a candidate is saved, also give the token access to `job-alerts` with the permission **Actions: read and write**. Without it everything still works; the welcome mail then goes out with the next hourly run.

## How it runs

- `tools/matrix.py` reads the candidates' files and lists who gets which alert. Each workflow then runs one job per candidate (the LinkedIn report also one search job per candidate and place).
- Every run is for exactly one candidate: `--profile candidates-data/candidates/<id>.json` replaces the built-in settings with that person's (see `apply_profile` in `job_match_runner.py`).
- What each candidate has already been sent is kept per candidate in the Actions cache, so nobody is mailed the same opening twice.
- `hourly-timer.yml` starts the LinkedIn report and the career-site scan once an hour; `fast-alert.yml` restarts itself; `timer-watchdog.yml` restarts either chain if it stops.

To stop everything: disable "Timer Watchdog", then cancel the waiting "Hourly Timer" and "Fast Alert" runs.

### The AI reader (optional, free)

The scans recognise skills from a fixed list of about 220 names. A posting can name any product or tool, so `brain.py` can also have a free AI model read each posting and name the skills it asks for; skills no list has then count towards the match percentage. It is off until a key is set.

To switch it on, create a free key and save it as a secret here (Settings -> Secrets and variables -> Actions):

| Secret | Where the free key comes from | What is known about the free allowance |
|---|---|---|
| `GEMINI_API_KEY` | Google AI Studio (aistudio.google.com -> Get API key) | Google does not publish it; it shows the live limits inside AI Studio. Tried first: `gemini-3.5-flash-lite` (answers in under a second), then `gemini-3.6-flash`. |
| `GROQ_API_KEY` | Groq (console.groq.com -> API Keys) | Published: 1,000 requests and 200,000 tokens a day per model, 8,000 tokens a minute. Roughly 130 postings a day per model, three models. |

Then run the **AI Reader Check** workflow: it asks every configured model to read one sample posting and lists which of them answer.

How it behaves:

- **Fastest model that reads correctly first, then the next.** (Measured on 2026-11-21: the two newest free Gemini models did not answer within 40 seconds, so they are left out; the lists can be changed, see below.) When a model has used up its free allowance the next one is used; when all are used up the postings left are read by the built-in list alone. A scan never fails or waits long for it (at most two minutes an hour, 40 seconds in a fast-alert pass, 40 postings a run).
- **Only postings still in the running are sent**: right title, right place, and years that do not already rule the candidate out.
- **Only the posting's title and text are sent.** Nothing about any candidate is. On Google's free plan, what is sent may be used to improve Google's products; a job posting is public text.
- **The model is not trusted blindly.** A skill counts only when the posting's own text contains it. The years are still read by the rules; the model's figure is used only where the rules found no figure at all.
- **Models are retired often.** If a log says a model is no longer served, put the current model names (comma-separated, best first) in the repository variables `BRAIN_GEMINI_MODELS` / `BRAIN_GROQ_MODELS`; no code change is needed.

Expect match percentages to move once it is on: a posting that asks for five tools the list never knew now shows them as missing.

### Limits to know about

- **GitHub Actions is for the owner and a few friends.** GitHub's terms do not allow running a paid service on it, and a free account runs at most 20 jobs at once.
- **About 10 to 12 candidates with all three alerts on.** Each fast alert holds one of the 20 job slots all day; each candidate's hourly work is about 18 minutes of jobs (a search per place at 2 to 7 minutes, a report, a career-site scan). At 12 candidates the hourly work finishes in about half an hour; at 16 it no longer fits in the hour and the runs queue up. With the fast alert off for most people the same account carries more, until LinkedIn starts refusing the searches. These figures are worked out from measured job times, not from a load test.
- **Each candidate is a separate set of requests** to LinkedIn and to the career sites. The scans are polite (pauses, time limits, and they stop when refused), but the load grows with every candidate.
- **A resume is not proof.** The page guesses years of experience from the resume's wording and dates and shows both; the saved figure is whatever is in the field.

## Career sites read

NTT DATA, Amazon, Cognizant, Tiger Analytics, Infor, JPMorgan Chase, Capgemini, LTIMindtree, EY, Tech Mahindra, Wipro, Infosys and Accenture.

**Job boards (65 more companies).** Many companies publish their openings through their hiring system's public API (Greenhouse, Lever, Ashby, SmartRecruiters), which exists so that careers pages and other programs can read them. One request returns a company's whole list, so reading it costs the same for one candidate or a thousand. The companies read this way are listed in `BOARDS` in `company_portals.py` (Okta, Zscaler, Databricks, MongoDB, Stripe, Paytm, Meesho, CRED, Razorpay, Bosch, ServiceNow, Freshworks, OpenAI and others); each had openings in India when it was added. To add one, find the company's name in its careers-page address (for example `boards.greenhouse.io/<name>` or `jobs.lever.co/<name>`) and add a line. Not covered, and named as such in every career-site mail: EPAM, Cimpress, Sify and Hetero (their job boards do not answer a script).

Tech Mahindra jobs have no link of their own: the row links to the openings page and the "Posted" cell gives the job reference number to type into "Job Reference ID" there.

## Developing

```
pip install -r requirements.txt
python tests/test_profiles.py          # candidate files, the general rules, the candidate list
python tests/test_role_fit.py          # the Python developer rules, score, duplicates
python tests/test_experience.py        # the years-of-experience reader
python tests/test_pipeline.py          # report, mail and failure paths
python tests/test_fast_alert.py
python tests/test_company_portals.py
node tests/test_page.js                # the settings page's resume reader
python tools/build_page_data.py        # after changing skills, cities or career sites: rewrites docs/data.js
```

A scan can be tried for one candidate without sending anything:

```
python company_portals.py --profile path/to/candidate.json --dry-run
python job_match_runner.py --profile path/to/candidate.json --fast-alert --dry-run
```
