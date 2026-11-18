# PlanWell — build spec (shared contract for all agents)

PlanWell is a personal, accessibility-first **pre-activity watcher** for someone whose daily activity limits change from day to day.
The user's only regular activities are **walking** and **pilates**, and every activity is in
Google Calendar. About 60 minutes before a calendar walk/pilates event, PlanWell compares
today's body state (from Oura) to the user's own past sessions on similar days and gives a
green / yellow / red verdict with plain-language reasons and gentle, specific options
(go as planned, shorten, swap to a gentler form, move earlier, add a rest buffer).

Non-goals: medical diagnosis or certainty, auto-editing the calendar without approval, multi-user.

## Hard rules
- **Python standard library only** (Python 3.9+ compatible, runs on macOS). No pip installs. No frameworks.
- **Front end: plain HTML/CSS/JS**, no build step, no CDN dependencies (inline SVG charts).
- **No LLM touches Oura data.** Oura's API agreement prohibits feeding its data into AI models.
  Agents build and test against **mock data only**. Never open `secrets/` or `data/planwell.db`.
- Tone: supportive, non-judgmental, never shaming for changing plans; never claim medical certainty.
- Show data freshness everywhere (Oura data can lag hours behind the ring).

## Layout
```
planwell/                      repo root (run everything from here)
  oura_connect.py              exists: one-time Oura OAuth login (writes secrets/oura_tokens.json)
  config.json                  user settings (see below)
  planwell/__init__.py
  planwell/db.py               SQLite schema + connect(path) helper           [DATA agent]
  planwell/oura_client.py      token load/refresh (single-use refresh tokens!) + paginated GET  [DATA]
  planwell/sync.py             `python3 -m planwell.sync [--days 365]` incremental pull into data/planwell.db  [DATA]
  planwell/mock_data.py        `python3 -m planwell.mock_data` -> data/mock.db, same schema, realistic POTS-like data  [DATA]
  planwell/gcal.py             Google Calendar OAuth (installed-app, loopback) + list events   [ENGINE]
  planwell/classify.py         calendar event -> "walking" | "pilates" | None (keywords from config)  [ENGINE]
  planwell/engine.py           features, HR zones, similar-days matching, verdict, options   [ENGINE]
  planwell/watcher.py          `python3 -m planwell.watcher [--once|--loop]` pre-activity checks + notifications  [ENGINE]
  planwell/server.py           `python3 -m planwell.server [--mock] [--port 8765]` serves web/ + JSON API  [ENGINE]
  web/index.html, web/styles.css, web/app.js, web/sample/*.json   [UI agent]
  data/   (gitignored)   secrets/ (gitignored)
```

## config.json
```json
{
  "hr_threshold_bpm": 125,
  "zones_bpm": [100, 115, 125, 140],
  "caution_style": "balanced",
  "precheck_minutes": 60,
  "activity_keywords": {
    "walking": ["walk", "stroll", "hike", "steps"],
    "pilates": ["pilates", "reformer", "mat class", "barre"]
  },
  "notify": {"ntfy_topic": "", "macos": true},
  "google_calendar_ids": ["primary"],
  "timezone": "America/New_York"
}
```
`zones_bpm` are upper bounds of Z1..Z4; above the last = Z5. Threshold and zones are user-set
(defaults are placeholders, not medical guidance). `caution_style`: cautious | balanced | flexible.

## SQLite schema (planwell/db.py) — identical in planwell.db and mock.db
All timestamps ISO-8601 strings with offset as Oura returns them. `day` = YYYY-MM-DD.
```
workouts(id TEXT PRIMARY KEY, activity TEXT, day TEXT, start_datetime TEXT, end_datetime TEXT,
         intensity TEXT, calories REAL, distance REAL, source TEXT, label TEXT, raw_json TEXT)
heartrate(timestamp TEXT PRIMARY KEY, bpm INTEGER, source TEXT)
daily_readiness(day TEXT PRIMARY KEY, score INTEGER, temperature_deviation REAL,
                resting_heart_rate_contrib INTEGER, hrv_balance_contrib INTEGER, raw_json TEXT)
daily_sleep(day TEXT PRIMARY KEY, score INTEGER, raw_json TEXT)
sleep(id TEXT PRIMARY KEY, day TEXT, type TEXT, bedtime_start TEXT, bedtime_end TEXT,
      total_sleep_duration INTEGER, average_hrv REAL, average_heart_rate REAL,
      lowest_heart_rate INTEGER, efficiency INTEGER, raw_json TEXT)
daily_stress(day TEXT PRIMARY KEY, stress_high INTEGER, recovery_high INTEGER, day_summary TEXT, raw_json TEXT)
daily_activity(day TEXT PRIMARY KEY, score INTEGER, steps INTEGER, active_calories INTEGER,
               high_activity_time INTEGER, medium_activity_time INTEGER, low_activity_time INTEGER, raw_json TEXT)
sync_state(endpoint TEXT PRIMARY KEY, last_synced_at TEXT, last_day TEXT)
```
Oura v2 base: `https://api.ouraring.com/v2/usercollection/{workout|heartrate|daily_readiness|daily_sleep|sleep|daily_stress|daily_activity}`.
`heartrate` takes `start_datetime`/`end_datetime` (max ~30 days per request; chunk it), others take
`start_date`/`end_date`. All paginate via `next_token`. Token endpoint `https://api.ouraring.com/oauth/token`.
Oura activity names are lowercase (e.g. "walking", "pilates"); keep raw value in `activity`.
For sleep, use only `type == "long_sleep"` as the main night when computing baselines.

## Engine definitions
- **Session metrics** (per workout, from heartrate rows within start..end, + 30 min after):
  avg_hr, peak_hr, minutes_above_threshold, minutes_per_zone[5], recovery_drop_bpm
  (peak − HR ~10 min after end, if available), duration_min, hr_coverage (fraction of minutes with samples).
- **Day features** (for the day of the session/check): readiness, sleep_score, total_sleep_min,
  hrv (long_sleep average_hrv), hrv_delta_pct vs trailing 30-day median, rhr (lowest_heart_rate)
  and rhr_delta vs 30-day median, stress_high minutes, steps_before (approximate if needed),
  hour_of_day, temperature_deviation. Missing values must be handled gracefully.
- **Similar days**: same activity; weighted, standardized distance on day features; k = 5–8.
  Outcome = minutes_above_threshold normalized per 30 min, compared with the user's typical
  (median) for that activity. Also consider scheduled duration.
- **Verdict**: green / yellow / red. Combine similar-days outcome with simple guard rails
  (e.g. very low readiness or large HRV drop raises caution). `caution_style` shifts cutoffs.
  With < 5 past sessions of that activity, fall back to rules and say so ("still learning").
- **Options** (specific, lowest disruption first): go as planned; shorten to N min (choose N where
  similar sessions stayed calmer); swap (walking -> shorter/flat/indoor walk; pilates -> mat/floor-only,
  fewer standing transitions); move earlier (if history shows earlier times go better); add 15-min rest buffer.
- Reasons: 2–4 short plain-language strings, e.g. "HRV is 18% below your usual".

## JSON API (server.py) — the UI depends on exactly these shapes
`GET /api/today`
```json
{
  "generated_at": "2026-11-10T16:00:00-05:00",
  "mode": "mock",
  "freshness": {"last_sync": "...", "latest_hr_sample": "...", "minutes_old": 42},
  "body": {"readiness": 64, "sleep_score": 71, "total_sleep_min": 340,
           "hrv": 38, "hrv_baseline": 46, "hrv_delta_pct": -17.4,
           "rhr": 62, "rhr_baseline": 58, "rhr_delta": 4,
           "stress_summary": "stressful", "steps_so_far": 3120, "temperature_deviation": 0.2},
  "day_status": "yellow",
  "day_headline": "A gentler day — your body is a bit below its usual.",
  "upcoming": [ CHECK, ... ],
  "settings": { ...config.json... }
}
```
`CHECK` object:
```json
{
  "event_id": "abc", "title": "Pilates w/ Sara", "start": "...", "end": "...",
  "duration_min": 50, "activity": "pilates",
  "check_due": "...", "status": "upcoming|checked",
  "verdict": "green|yellow|red", "confidence": "learning|moderate|good",
  "headline": "Take it easy today",
  "reasons": ["HRV is 17% below your usual", "You slept 5h 40m"],
  "expected": {"minutes_above_threshold": 14, "typical_minutes_above_threshold": 4, "peak_hr": 138},
  "similar_days": [{"workout_id": "w1", "date": "2026-10-02", "duration_min": 50, "avg_hr": 112,
                    "peak_hr": 141, "minutes_above_threshold": 16, "readiness": 61,
                    "hrv_delta_pct": -20.1, "similarity": 0.86}],
  "options": [{"type": "shorten|swap|move|buffer|go", "label": "Shorten to 30 min",
               "detail": "On similar days, sessions ≤30 min stayed under 125 bpm.",
               "new_start": null, "new_end": null}]
}
```
`POST /api/check` body `{"activity":"walking","start":"ISO","duration_min":30}` -> CHECK (manual "check now").
`GET /api/sessions?activity=pilates&limit=60` -> `{"sessions":[{workout_id,date,start,activity,duration_min,avg_hr,peak_hr,minutes_above_threshold,minutes_per_zone:[..5],recovery_drop_bpm,readiness,hrv_delta_pct}], "zones_bpm":[...], "hr_threshold_bpm":125}`
`GET /api/session/<workout_id>` -> `{"session":{...as above}, "hr":[{"t":"ISO","bpm":110},...]}`
`GET /api/settings` / `POST /api/settings` (partial update, persists config.json).
Errors: `{"error": "..."}` with non-200 status.

## Notifications (watcher.py)
Runs every ~10 min (launchd on macOS; provide `scripts/install_launchd.sh`). For each walking/pilates
event starting in `precheck_minutes` ± 10, run a fresh `sync` of recent data then the check; notify once
per event (dedupe in a small `notified` table). Channels: macOS notification via `osascript`, and
ntfy (`POST https://ntfy.sh/<topic>`) if a topic is set. Notification text = headline + top reason + top option.

---
# Addendum v2 (Nov 11) — demo strengthening. Binding for all agents.

Context: all of the user's Oura "pilates" workouts are **Solidcore** classes (Oura has no Solidcore type).
Keep the internal activity key `pilates`; show the label "Solidcore" everywhere in the UI and messages.

## config.json additions (settings.py defaults + validation)
```json
"activity_labels": {"walking": "Walk", "pilates": "Solidcore"},
"preview": {"enabled": true, "time": "20:00"},
"late_cancel_hours": {"pilates": null, "walking": null}
```
`late_cancel_hours` is user-set (null = unknown; never assume a studio's policy).

## Night-before preview
- For each walking/pilates event tomorrow: `preview_due = min(today at preview.time, start - late_cancel_hours - 60 min)`
  (ignore the second term when late_cancel_hours is null). Watcher sends ONE combined "tomorrow" notification
  when the earliest preview_due for tomorrow is reached (dedupe key `preview@<date>` in `notified`).
  If a deadline is already past, say so kindly, without shaming.
- Verdicts use today's body data and are framed as "early look"; it re-checks 1 hour before as usual.
- CHECK gains optional fields: `"label": "Solidcore"`, `"cancel_by": "ISO or null"`, `"kind": "precheck|preview"`.
- New option types for pilates/Solidcore: `"rebook"` ("Book a later class", when history shows later times went
  calmer, or generically "a later class may suit") and `"modify"` ("Plan floor modifications": fewer standing transitions,
  take breaks between blocks). Walking keeps shorten/swap/move/buffer/go. Order: least disruptive first; on red, "go" last.

`GET /api/tomorrow`
```json
{"date": "2026-11-12", "generated_at": "...", "day_status": "yellow", "kind": "preview",
 "headline": "Tomorrow: an easier plan may suit",
 "note": "Based on today's body data. I'll re-check 1 hour before each session.",
 "preview_due": "ISO", "events": [CHECK, ...]}
```

## Analytics (planwell/analytics.py)
"Hard session" = minutes above threshold per 30 min ≥ the user's 75th percentile for that activity (min 3 min/30).
`insights(db_path, config) -> dict`, `backtest(db_path, config, min_history=8) -> dict`.

`GET /api/insights`
```json
{"generated_at": "...", "n_sessions": {"walking": 110, "pilates": 48},
 "metric": "minutes above 125 bpm per 30 min",
 "insights": [{"id": "sleep", "title": "Short sleep", "finding": "After nights under 6 h, Solidcore ran about 3× harder.",
   "activity": "pilates|walking|all",
   "a": {"label": "< 6 h sleep", "value": 9.2, "n": 11}, "b": {"label": "7 h +", "value": 3.1, "n": 20},
   "confidence": "low|medium|high"}],
 "caveat": "Patterns in your own history, not medical advice."}
```
Insight ids: `sleep`, `hrv`, `readiness`, `time_of_day`, `activity_compare` (walk vs Solidcore), `recovery_trend`.
Confidence from group sizes (n<6 low, <15 medium, else high). Skip an insight if either group has n<4.

`GET /api/backtest` — replay every past session in time order; judge each ONLY with data before it
(check as of 60 min before start, same engine). Skip sessions with < min_history earlier sessions of that activity.
```json
{"generated_at": "...", "n_sessions": 140, "method": "Each session judged only with data from before it.",
 "hard_definition": "≥ 6 min above 125 bpm per 30 min (your top 25%)",
 "planwell": {"tp": 20, "fp": 9, "fn": 6, "tn": 105, "precision": 0.69, "recall": 0.77, "flag_rate": 0.21},
 "baseline": {"name": "Readiness under 70 only", "tp": 0, "fp": 0, "fn": 0, "tn": 0, "precision": 0, "recall": 0, "flag_rate": 0},
 "by_verdict": [{"verdict": "green", "n": 90, "avg_minutes": 1.2, "pct_hard": 0.06}],
 "timeline": [{"workout_id": "..", "date": "2026-10-02", "activity": "pilates", "verdict": "yellow",
               "minutes_per_30": 8.1, "hard": true}]}
```
"Flagged" = verdict yellow or red. Report honestly even if the numbers are modest.

## Static demo (scripts/build_demo.py)
Generates the whole static demo from REAL ENGINE OUTPUT on mock data, at a fixed natural "now"
(the Wednesday before the build, 15:30 America/New_York), for three scenarios using `mock_data --today good|meh|poor`:
```
web/sample/scenarios/<good|meh|poor>/today.json, tomorrow.json, check_walking.json, check_pilates.json
web/sample/sessions.json, insights.json, backtest.json, settings.json, session_<id>.json (all sessions listed)
```
The mock calendar for the demo must have, on "today": a Solidcore class ~17:00 and a walk ~19:00 (after "now"),
and tomorrow: a morning walk and a Solidcore class. Old hand-written sample files are replaced.

---
# Addendum v3 (Nov 11) — any activity, classes, nightly sync, morning 48-hour view, AI event labels. Binding.

## Activity registry (replaces hard-coded walking/pilates)
config.json gains `activities` (settings.py migrates old `activity_keywords`/`activity_labels` into it; keep
reading the old keys for back-compat; `late_cancel_hours` moves into each activity):
```json
"activities": {
  "walking": {"label": "Walk", "kind": "exercise", "keywords": ["walk", "stroll", "hike"],
              "oura_types": ["walking", "hiking"], "traits": {"posture": "standing", "pattern": "steady", "setting": "either"},
              "late_cancel_hours": null},
  "pilates": {"label": "Solidcore", "kind": "exercise", "keywords": ["solidcore", "pilates", "reformer"],
              "oura_types": ["pilates"], "traits": {"posture": "mixed", "pattern": "bursty", "setting": "indoor"},
              "late_cancel_hours": null},
  "padel":   {"label": "Padel", "kind": "exercise", "keywords": ["padel"],
              "oura_types": ["padel", "paddle_tennis", "tennis", "racquet_sports"],
              "traits": {"posture": "standing", "pattern": "bursty", "setting": "either"}, "late_cancel_hours": null},
  "class":   {"label": "Class", "kind": "daily", "keywords": ["class", "lecture", "seminar", "ba7", "msba"],
              "oura_types": [], "traits": {"posture": "seated", "pattern": "still", "setting": "indoor"},
              "late_cancel_hours": null}
}
```
- Keys are lowercase slugs; users can add/edit/remove activities in Settings (POST /api/settings validates).
- **Exercise** sessions = Oura workouts whose `activity` is in `oura_types` (first matching activity wins).
- **Daily** sessions (e.g. class) = PAST CALENDAR EVENTS matching the keywords, with heart rate from the
  `heartrate` table during the event (daytime Oura HR is sparse, ~5-min; require hr_coverage ≥ 0.3, else skip).
  Past calendar events are stored locally: new table
  `calendar_events(id TEXT PRIMARY KEY, calendar_id TEXT, title TEXT, start TEXT, end TEXT, location TEXT, raw_json TEXT)`;
  `python3 -m planwell.gcal --history 180` and the nightly job fill it. Mock data fills it too.
- **Cold start**: with n own sessions, blend own-history expectation with a "neighbours" expectation from
  activities sharing traits (posture/pattern), weight w = n/(n+5). With n < 5 confidence = "learning" and the UI says
  "Still learning padel — using your similar activities for now". Classify uses label words in all messages.
- CHECK.label and all copy use the activity label; option sets by trait: bursty → "modify" (take more breaks /
  shorter rallies or floor mods), rebook for anything with late_cancel_hours set or kind exercise booked classes,
  shorten/swap/move/buffer/go as before. Daily-kind activities never get "rebook"; they get "buffer" (rest block
  after), "modify" ("sit when you can", "bring water/salt per your own plan" is NOT allowed — no medical advice;
  use "plan a seated break") and "go".
- Analytics (insights, backtest) and History iterate over all activities; insights skip small groups as before.

## AI event labels (planwell/labeler.py) — calendar text only
`label_event(event, config) -> {"category": "<activity key or 'other'>", "posture": "seated|standing|mixed|floor",
 "pattern": "still|steady|bursty", "setting": "indoor|outdoor|either", "exertion": 1-5, "source": "ai|rules|cache"}`
- Input whitelist: title, location, duration_min, local start hour, weekday. NOTHING else — never Oura data, never
  verdicts or body numbers. Enforce with a single function that builds the payload from those fields only, and a
  unit test asserting the payload keys.
- config `labeler`: `{"provider": "none|anthropic|vertex", "model": "claude-haiku-5-5", "vertex_project": "your-gcp-project",
  "vertex_region": "us-east5"}`; default provider "none" → keyword/rules labeller. Anthropic: POST
  https://api.anthropic.com/v1/messages (x-api-key from env ANTHROPIC_API_KEY or secrets/anthropic_key.txt,
  anthropic-version 2023-06-01). Vertex (uses BA780 credits): POST
  https://{region}-aiplatform.googleapis.com/v1/projects/{project}/locations/{region}/publishers/anthropic/models/{model}:rawPredict
  with bearer from `gcloud auth print-access-token`, body includes "anthropic_version": "vertex-2023-10-16".
  Ask for strict JSON; validate; on any failure fall back to rules. Cache results in table
  `event_labels(key TEXT PRIMARY KEY, label_json TEXT, created_at TEXT)` keyed by sha256 of the payload.
- Events not matching any activity keyword but labelled exertion ≥ 3 by AI are shown as "unrecognised activity"
  with a one-tap "Track this as …" suggestion in the UI; they are not checked.

## Nightly sync + morning 48-hour view
- New launchd jobs (scripts/install_launchd.sh installs all, uninstall removes all): nightly at 03:30
  `python3 -m planwell.nightly` = Oura sync (recent 3 days) + calendar history refresh (last 14 days + next 3);
  morning digest at `config.morning.time` (default "07:00") = build next-48h and send one notification
  (dedupe `morning@<date>`). Watcher keeps doing 1-hour prechecks and the night-before preview.
- `engine.build_next48(db_path, config, events, now=None)`; `GET /api/next48`:
```json
{"generated_at": "...", "days": [
  {"date": "2026-11-11", "label": "Today", "load": {"expected": 34, "typical": 22, "level": "light|usual|heavy|very_heavy"},
   "headline": "A heavier day than usual",
   "items": [{"event_id": "..", "title": "BA780 class", "start": "...", "end": "...", "activity": "class",
              "label": "Class", "check": CHECK_or_null, "ai_label": LABEL_or_null, "tracked": true}],
   "suggestions": [{"type": "space|rest_block|move|shorten", "text": "Solidcore yesterday + padel today has been hard before. Consider padel on Friday.",
                    "event_id": "..or null"}]}
 ], "note": "Tomorrow's checks are early looks; each gets a fresh check 1 hour before."}
```
- load.expected = sum over tracked items of expected minutes above threshold (checks) + daily-kind items' expected;
  typical = median daily total over the last 60 days (from history). level by ratio (<0.7 light, ≤1.2 usual,
  ≤1.6 heavy, else very_heavy). Also add a day feature `load_yesterday` (sum of yesterday's session minutes above
  threshold) to the similar-days features (low weight) and a suggestion when two high-load days are back-to-back.
- Morning notification text: "Today: heavier than usual — Class 9:30, Padel 7:00 pm (go gently). Tomorrow: usual."
