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

## How it runs

- `tools/matrix.py` reads the candidates' files and lists who gets which alert. Each workflow then runs one job per candidate (the LinkedIn report also one search job per candidate and place).
- Every run is for exactly one candidate: `--profile candidates-data/candidates/<id>.json` replaces the built-in settings with that person's (see `apply_profile` in `job_match_runner.py`).
- What each candidate has already been sent is kept per candidate in the Actions cache, so nobody is mailed the same opening twice.
- `hourly-timer.yml` starts the LinkedIn report and the career-site scan once an hour; `fast-alert.yml` restarts itself; `timer-watchdog.yml` restarts either chain if it stops.

To stop everything: disable "Timer Watchdog", then cancel the waiting "Hourly Timer" and "Fast Alert" runs.

### Limits to know about

- **GitHub Actions is for the owner and a few friends.** GitHub's terms do not allow running a paid service on it, and a free account runs at most 20 jobs at once. With many candidates the jobs queue; beyond roughly ten to fifteen candidates the hourly scans stop fitting in the hour.
- **Each candidate is a separate set of requests** to LinkedIn and to the career sites. The scans are polite (pauses, time limits, and they stop when refused), but the load grows with every candidate.
- **A resume is not proof.** The page guesses years of experience from the resume's wording and dates and shows both; the saved figure is whatever is in the field.

## Career sites read

NTT DATA, Amazon, Cognizant, Tiger Analytics, Infor, JPMorgan Chase, Capgemini, LTIMindtree, EY, Tech Mahindra, Wipro, Infosys and Accenture. Not covered, and named as such in every career-site mail: EPAM, Cimpress, Sify and Hetero (their job boards do not answer a script).

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
