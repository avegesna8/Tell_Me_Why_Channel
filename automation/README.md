# Queue → long video + Short → thumbnail/metadata → email → scheduled uploads

Production requires cloud storage, provider credentials, SMTP and YouTube OAuth setup.
Keep ENABLE_VIDEO_AUTOMATION disabled until configuration is complete.
This pipeline uses its own private Tell_Me_Why_Channel GitHub repository,
separate from the RunPod voice-worker repository.
Never include `.env`, OAuth files, `.venv`, or `runs` in the repository.

## Queue and timing

Edit `video_queue.json`. Items are processed in array order, one per workflow run.
Stable IDs must never be reused for different ideas. An unfinished first item
blocks later items. Remove/disable an item only when intentionally skipping it.
Completed items remain in JSON but are skipped using persistent state.

Example item (choose real future dates; these are illustrative):

```json
{
  "id": "imax-explained-001",
  "idea": "What does IMAX mean?",
  "notes": "Explain how the experience differs between theaters. Avoid implying every IMAX uses film or the same screen size.",
  "minutes": 3,
  "enabled": true,
  "made_for_kids": false,
  "contains_synthetic_media": false,
  "publish_at": "2026-11-10T17:00:00-05:00",
  "short_publish_at": "2026-11-11T12:00:00-05:00"
}
```

`idea` is the required topic or question. `notes` is optional free-form guidance
for the script writer, such as facts or examples to include, desired tone and
structure, audience context, or claims to avoid. Notes are passed to the writer
as guidance and are not read aloud verbatim unless that fits the resulting script.

Timezone is America/New_York (Eastern). Supply explicit timestamp offsets:
-04:00 during daylight saving and -05:00 during standard time. Upload times must
be at least 15 minutes in the future when scheduling runs. Expired times stop the
job rather than accidentally publishing immediately. The production cron and
YouTube publish timestamps are independent. The checked-in cron, 13:17 UTC daily,
is a provisional production time (09:17 EDT / 08:17 EST), not a requested cadence.
Change it before enabling; Actions schedules can be delayed.

`email_mode: review` (current default) generates both videos and sends the
thumbnail, scripts and metadata as attachments to abhinn.varma@gmail.com. The
email links to the workflow run containing video artifacts. It then waits:
open the approval workflow link in the email, select **Run workflow**, and paste
the provided `approve_id` and `approval_code`. The form also lets you optionally
override the long-video and Short publication times before approval. Use ISO 8601
with an explicit Eastern offset, such as `2026-10-10T17:00:00-04:00`; a blank field
uses that video's time from `video_queue.json`. GitHub sign-in and workflow-run
permission are required. The code binds approval to the exact videos, scripts,
metadata, thumbnail, audience declarations and publishing times. Edits invalidate
approval and cause a new review email. The legacy queue `approved` field is ignored.
Both videos upload privately, then get scheduled, then a confirmation email
includes their YouTube links. The channel owner can inspect private videos in
YouTube Studio. Setting `email_mode: confirmation` skips approval and schedules
automatically, then emails the details. Do not enable until that is your preference.

Approval uses the authenticated GitHub workflow form, not an email reply integration.
Both MP4 files are attached when their combined raw size is at most 14 MiB; larger
videos are included in the workflow artifact linked from the email. Other attachments
include both scripts, metadata and the thumbnail. Artifacts appear when the workflow
finishes and expire after 14 days; private repository access requires GitHub sign-in. If publication times
have become stale while waiting for review, update them and run production again to receive a fresh review email before approval.
Once any upload/scheduling starts, do not change metadata or times without
reconciling the saved state and YouTube Studio.

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
an item has been completed for seven days and both YouTube schedules are saved.
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
- SUMMARY_EMAIL_TO: optional override; queue defaults to abhinn.varma@gmail.com.

Repository Secrets:

- OPENAI_API_KEY, RUNPOD_API_KEY
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
remove that restriction. Confirm your project's eligibility for scheduled public
publishing. Custom-thumbnail permission must be available on your channel.

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
If one schedule succeeds and the other fails, the first may still publish at its
saved time; inspect both in Studio if an error is not resolved before publication.

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
`regenerate`. Enter the item ID and package code from that review email.
Regenerate starts a completely fresh script, long video, Short, images, thumbnail
and metadata for the same idea. New versions are saved under
`projects/ITEM_ID/revision-002/`, then `revision-003/`, etc. Previous files and
review state are retained. New versions always require fresh review approval.
Generation incurs the normal provider costs again.

Retrying the same regeneration request resumes its version instead of starting
another one. A stale code cannot regenerate a newer version. Regeneration is
allowed before YouTube uploads begin; it does not delete, unschedule, or replace
videos already on YouTube. Email replies still are not monitored; use the linked
GitHub workflow form. Keep publishing dates in the future before approving.

## Small manual review tests

Queue items may set `short_seconds` from 30 to 60 (default 55). Main duration uses
`minutes`. The manual workflow defaults to `review_only: true`: it can run while
scheduled production is disabled and cannot upload or schedule YouTube videos,
even if approval inputs are provided. Choose real publishing times and disable
review-only when approving an emailed package after production is configured.
For Shorts under 45 seconds, audio is fitted to the requested target within the
same 0.8–1.25 speed limits; final duration must be within one second of the target.

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
