# Queue → videos → owner review → private/draft multi-platform uploads

Production requires cloud storage, provider credentials, SMTP and YouTube OAuth setup.
Keep ENABLE_VIDEO_AUTOMATION disabled until configuration is complete.
This pipeline uses its own private Tell_Me_Why_Channel GitHub repository,
separate from the RunPod voice-worker repository.
Never include `.env`, OAuth files, `.venv`, or `runs` in the repository.

## Local queue dashboard

Double-click `start_queue_dashboard.command`, or run:

```sh
.venv/bin/python queue_web.py
```

The server binds only to `127.0.0.1` and opens `http://127.0.0.1:8765` in the
default browser. The form validates an idea, creates a stable unique ID, edits only
`video_queue.json`, commits that file and pushes `main`. Retrying the same browser
submission is idempotent. Existing manually edited queue entries appear on the page
and can be validated, committed and pushed with one button. Other working-tree files
are never staged. After a successful push, use the page's production-workflow link
to start the manual review-only run.

## Queue and timing

Edit `video_queue.json`. Items are processed in array order, one per workflow run.
Stable IDs must never be reused for different ideas. An unfinished first item
blocks later items. Remove/disable an item only when intentionally skipping it.
Completed items remain in JSON but are skipped using persistent state.

Example item:

```json
{
  "id": "imax-explained-001",
  "idea": "What does IMAX mean?",
  "notes": "Explain how the experience differs between theaters. Avoid implying every IMAX uses film or the same screen size.",
  "minutes": 3,
  "enabled": true,
  "made_for_kids": false,
  "contains_synthetic_media": false
}
```

`idea` is the required topic or question. `notes` is optional free-form guidance
for the script writer, such as facts or examples to include, desired tone and
structure, audience context, or claims to avoid. Notes are passed to the writer
as guidance and are not read aloud verbatim unless that fits the resulting script.

The GitHub Actions cron controls when the queue is checked. The checked-in cron,
13:17 UTC daily, is provisional; change it before enabling automatic production.
It does not publish videos publicly. Approved videos are uploaded immediately with
YouTube privacy set to `private`.

`email_mode: review` (current default) generates both videos and sends the
thumbnail, scripts and metadata as attachments to tellmewhy.contact25@gmail.com. The
email links to the workflow run containing video artifacts. It then waits:
open the approval workflow link in the email, select **Run workflow**, and paste
the provided `approval_code`. GitHub sign-in and workflow-run
permission are required. The code binds approval to the exact videos, scripts,
metadata, thumbnail and audience declarations. Edits invalidate
approval and cause a new review email. The legacy queue `approved` field is ignored.
Both videos upload privately, then a confirmation email includes their YouTube
links. They remain private until the channel owner changes visibility in YouTube
Studio. Setting `email_mode: confirmation` skips approval, uploads privately and
then emails the details. Do not enable until that is your preference.

Approval uses the authenticated GitHub workflow form, not an email reply integration.
Both MP4 files are attached when their combined raw size is at most 14 MiB; larger
videos are included in the workflow artifact linked from the email. Other attachments
include both scripts, metadata and the thumbnail. Artifacts appear when the workflow
finishes and expire after 14 days; private repository access requires GitHub sign-in. Once any upload starts, do not change its files or metadata without reconciling
the saved state and YouTube Studio.

## GitHub-hosted runner and private cloud storage

Production runs on `ubuntu-latest`; no server is required. Workload Identity
Federation authenticates GitHub to the private GCS bucket without a service-account
key. The service account needs Storage Object Admin on that bucket only.

Upload the clean reference WAV as `config/reference.wav` in the bucket. The workflow
downloads it outside the checkout. Commit `characters/updated_base.png` to GitHub.
Run **Check cloud storage connection** manually before enabling production; this
only creates, reads and deletes a small probe object. It makes no AI, email or
YouTube calls.

`pipeline/state.json` and `pipeline/projects/` retain queue state and assets between
runs. Pipeline atomic writes and RunPod job records checkpoint synchronously to
GCS; assets are synced before a queue transition, email or upload can proceed.
A failed checkpoint stops processing. Interrupted image/API responses may still
incur charges without a saved result; they cannot provide exactly-once generation.
Render intermediates are rebuilt when needed. Do not publish state as artifacts:
it can contain private resumable-upload URLs.

GitHub concurrency and a GCS create-only lock prevent competing production runs.
If the runner is forcibly terminated, `locks/queue.json` may remain. Check the
recorded GitHub run is no longer running, reconcile any ambiguous RunPod/email/
YouTube operation, then delete only that lock object to permit a retry. Never
remove a lock while its run is active. Normal errors release the lock.

On each production run, project media (including older revisions) is deleted once
an item has been completed for seven days and both YouTube video IDs are saved.
Awaiting-review and failed items are retained. Small state records and YouTube IDs
remain. Cleanup runs only while automation is enabled and runs successfully far
enough to acquire storage. Bucket soft-delete/versioning policies can retain deleted
bytes longer. Do not add a bucket-wide age lifecycle rule: it could delete pending
reviews, the reference voice or durable state. GitHub review artifacts independently
expire after 14 days.

## GitHub settings

Repository Variables:

- ENABLE_VIDEO_AUTOMATION: `true` only after setup; unset disables all jobs.
- GCP_WORKLOAD_IDENTITY_PROVIDER: full provider resource name.
- GCP_SERVICE_ACCOUNT: service-account email.
- GCS_BUCKET: private bucket name (without `gs://`).
- RUNPOD_ENDPOINT_ID: tested voice endpoint.
- RUNPOD_MODEL_VERSION: `2` or `2.5` matching the tested worker. IndexTTS-2.5 passed a short synthesis test; set this to `2.5` for the upgraded endpoint.
- Reference voice: stored at `gs://BUCKET/config/reference.wav`; the workflow sets its local path.
- SMTP_HOST: your authenticated SMTP provider; SMTP_PORT currently 587/STARTTLS.
- SMTP_FROM: permitted sender address.
- SUMMARY_EMAIL_TO: optional override; workflow uses tellmewhy.contact25@gmail.com.

Repository Secrets:

- OPENAI_API_KEY, RUNPOD_API_KEY

Optional repository variables for script review:

- OPENAI_PLAN_MODEL: writer/planner model; defaults to `gpt-4.1-mini`.
- OPENAI_REVIEW_MODEL: single-pass reviewer; defaults to the planning model.
- SCRIPT_WEB_FACT_CHECK: keep `0` for the low-cost default; set `1` to allow web search in that same review call.
- SMTP_USERNAME, SMTP_PASSWORD (an app password if supported by your provider)
- YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN

Receiving email at Gmail does not require sending through Gmail. If using Gmail
SMTP, configure smtp.gmail.com and an eligible account's app password. No SMTP
password or OAuth token belongs in the queue JSON or workflow source.

## YouTube OAuth

Enable YouTube Data API v3 in your Google Cloud project. Configure the OAuth
consent screen, create a Desktop OAuth client, and download its JSON privately.
Authorize the actual channel owner locally:

```sh
.venv/bin/python -m pip install google-auth-oauthlib
.venv/bin/python -m automation.authorize_youtube --client-secrets /private/path/client.json --out /private/path/youtube-secrets.json
```

Copy values to GitHub Secrets and keep the output outside this repository.
Google's consent/testing configuration can limit refresh-token lifetime.
Uploads from unverified API projects can be restricted to private viewing until
YouTube's required audit is completed; a successfully built pipeline does not
remove that restriction. No public scheduling is performed. Custom-thumbnail permission may require
additional channel eligibility; if YouTube rejects the thumbnail, both videos still
upload privately and the confirmation email tells you to add it manually.

Every approved upload is explicitly configured as private with English metadata
and spoken audio (`en`), no paid product placement, embedding allowed, the standard
YouTube license, public statistics visible, and the queue's made-for-kids and
synthetic-media declarations. The Education category remains the queue default.

The long video gets a generated reference-based 1280×720 thumbnail. The Short
gets its own title/description/tags, but no custom thumbnail is applied through
this workflow; choose its cover in YouTube's supported Shorts interface.
Set made_for_kids and contains_synthetic_media deliberately for each video pair;
these are owner declarations, not model-generated guesses.

## Failure/retry behavior

RunPod saved jobs are resumed. Matching generated assets are cached. Uploads use
persisted resumable session URLs; lost upload responses are checked on retry.
Expired/ambiguous sessions stop for manual reconciliation rather than starting a
second upload. SMTP cannot guarantee exactly-once delivery: an interrupted send
leaves email_*=`sending`; inspect the inbox before marking it `sent` or clearing
that field. Do not clear YouTube upload state without inspecting Studio first.

Retry by manually dispatching the workflow. It resumes the first unfinished
item. State is at VIDEO_WORK_ROOT/state.json; generated files are under
VIDEO_WORK_ROOT/projects/ITEM_ID. Review artifacts are retained for 14 days;
cloud originals remain until completed-item cleanup described above.

Validate without external calls:

```sh
.venv/bin/python -m automation.queue_runner --work-root /tmp/queue-check --validate-only
.venv/bin/python -m unittest discover -s tests -q
```

References:
- https://developers.google.com/youtube/v3/guides/using_resumable_upload_protocol
- https://developers.google.com/youtube/v3/docs/videos
- https://developers.google.com/youtube/v3/docs/thumbnails/set
- https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows

## Regenerate from the review email

The same emailed workflow link now offers `review_action: approve` or
`regenerate`. Enter the package code from that review email.
Regenerate starts a completely fresh script, long video, Short, images, thumbnail
and metadata for the same idea. New versions are saved under
`projects/ITEM_ID/revision-002/`, then `revision-003/`, etc. Previous files and
review state are retained. New versions always require fresh review approval.
Generation incurs the normal provider costs again.

Retrying the same regeneration request resumes its version instead of starting
another one. A stale code cannot regenerate a newer version. Regeneration is
allowed before YouTube uploads begin; it does not delete or replace videos already on YouTube. Email replies still are not monitored; use the linked
GitHub workflow form.
## Small manual review tests

Queue items may set `short_seconds` from 30 to 60 (default 55). Main duration uses
`minutes`. The manual workflow defaults to `review_only: true`: it can run while
scheduled production is disabled and cannot upload YouTube videos, even if
approval inputs are provided. Disable review-only when approving an emailed package
after production is configured; approval uploads both videos privately.
For Shorts under 45 seconds, audio is fitted to the requested target within the
same 0.8–1.25 speed limits; final duration must be within one second of the target.

## Low-cost script quality gate

Before storyboard, narration or image generation, each new long-form script gets
free deterministic checks plus one cached structured editorial review using
`OPENAI_REVIEW_MODEL`, or `OPENAI_PLAN_MODEL` when no separate reviewer is set.
The reviewer may return one polished revision in that same call. There is no
multi-pass LLM loop. A retry reuses `script-quality.json` unless the script, brief,
model or review mode changes. The original draft remains in `script.draft.txt`.
The review requires all final scores to be at least 7/10, no remaining high-severity
issue, a reasonable word count, and narration-safe formatting before any audio or
images are requested. The report is included in the review email and artifact.

`SCRIPT_WEB_FACT_CHECK=0` is the default and avoids web-search tool charges. Set it
to `1` only when source-grounded fact checking is worth the extra tool cost; it
still uses the same single review response. Without web search, the reviewer is
instructed to qualify or remove claims it cannot confidently support rather than
claiming independent verification. Shorts use the reviewed long script as their
source and retain their existing duration and narration checks without another
editorial LLM call.

## Current voice and creative direction

The local reference is `voices/Liam_Final-reference.wav`, converted from the first
15 seconds of `voices/Liam_Final.mp3` to 24 kHz mono PCM WAV. `.env` points to this
reference. For GitHub production, replace `config/reference.wav` in the private
bucket with this WAV (keeping that cloud object name). Voice files stay out of Git.

New scripts open with a specific curiosity hook and close with a memorable payoff
that connects to the opening. New thumbnails emphasize one striking, truthful
visual idea, strong contrast, mobile readability and optional 2–4-word hook text.
Existing review packages are preserved; these changes do not regenerate them.

## Automatic optional music and sound effects

`AUDIO_LIBRARY` enables the library mixer. The workflow downloads and discovers
files in the bucket's `audio/music/` and `audio/sfx/` folders before generation.
Future additions are available automatically; use descriptive filenames. See
`audio/README.md` for catalog hints and credits. Local runs use `audio/library.json`.
Unset AUDIO_LIBRARY to disable automatic selection; `--music` remains available
for manually chosen background music when the library option is disabled.

Each video gets one cached text-planning request after narration alignment. Music
and effects are both optional. Selected effects are bound to actual word timestamps,
with conservative count and spacing limits. FFmpeg mixes quiet normalized audio,
ducks music under the narrator and applies fades and peak limiting; video frames
and subtitle streams are copied. The original narration-only video is retained
as `video.narration.mp4` for retry-safe mixing. Choices appear in `soundtrack.json`,
the review report and the GitHub artifact. Required credits are added to metadata.
Existing ready/emailed packages are not regenerated by deploying these changes.


## Facebook, Instagram and TikTok staging

After approval, the long video and Short upload privately to YouTube. The long
video is also uploaded to the Tell Me Why Facebook Page as unpublished content,
and the Short is uploaded as a Facebook Reel draft. The Short is sent to the
authorized TikTok Sandbox target account with video.upload; open the TikTok
inbox notification to finish editing and publish it.

Instagram's Content Publishing API does not create a persistent native draft
visible in the Instagram app. The confirmation report therefore includes the
Instagram account, prepared caption and hashtags, and the Short remains available
as an email attachment or private workflow artifact for manual Instagram upload.
TikTok inbox drafts and unpublished Facebook content do not have public post URLs;
the confirmation report includes provider IDs, status, and the Meta Business Suite
management link.

Required GitHub Actions secrets for staging are META_PAGE_ACCESS_TOKEN,
TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET, and TIKTOK_REFRESH_TOKEN.
Set META_PAGE_ID and META_INSTAGRAM_ACCOUNT_ID as repository variables (the
workflow also accepts them as secrets). TikTok refresh-token rotation is persisted
in the private cloud checkpoint state after the first approved run.
