# Tell_Me_Why_Channel

Queue automation, GitHub Actions, email review and YouTube scheduling are
documented in [automation/README.md](automation/README.md). Configure
`video_queue.json`; the workflow is disabled until explicitly enabled in GitHub.

Turn a finished narration script into an editable storyboard, one generated image
per scene, one continuous RunPod IndexTTS-2 narration, and a captioned MP4. Defaults to
16:9 landscape at 1920×1080, with mostly still drawings and occasional gentle zooms.

## Setup

Requires Python 3.10+ and FFmpeg/FFprobe on PATH. Burned captions require FFmpeg
with libass (`subtitles` filter). On macOS: `brew install ffmpeg`.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Add the settings from `.env.example` to your `.env`; preserve your existing key.
Set `OPENAI_API_KEY`, `RUNPOD_API_KEY`, `RUNPOD_ENDPOINT_ID`, and
`RUNPOD_REFERENCE_AUDIO` (a local path to a short, clean speech recording).
RunPod is the default narration provider. The endpoint must be a queue-based
Serverless deployment of your `indextts-2-youtube/handler.py`. It receives
`input: {mode: "tts", text: "...", ref_audio_b64: "..."}` and must return
`{ok: true, format: "wav", audio_base64: "..."}` as its job output.
The reference is required because that backend's built-in fallback is a beep.
Keep reference audio below 5 MB. `--runpod-endpoint` and `--reference-audio`
can override the environment settings. No changes to the deployed worker are
required for this contract; this project does not deploy it automatically.

To use the legacy provider, pass `--tts-provider siliconflow` (or set
`TTS_PROVIDER=siliconflow`), with `SILICONFLOW_API_KEY` and `SILICONFLOW_VOICE`.
For SiliconFlow, use a model-qualified preset or uploaded `speech:` URI.
Model names can be overridden in `.env` for your account's model access.
Planning, image generation, and narration make billable provider requests.

## Generate a video

To create a long video followed by a separate Shorts companion:

```sh
.venv/bin/python pipeline.py run --idea "What does IMAX mean?" --out runs/new-topic --with-short
```

Or create only the companion from an existing saved storyboard:

```sh
.venv/bin/python pipeline.py short --out runs/imax-snappy --short-seconds 55
```

The Short adapts one central idea into a standalone script with an immediate
hook and payoff. New idea-generated long scripts also get stronger opening
guidance; existing scripts are not rewritten. Shorts use independent images
composed for 9:16, approximately four-second visual segments, the current voice
and character reference, continuous narration and the same caption style.
Output is `OUT/short/video.mp4` (1080×1920), with its own script, storyboard,
assets and timing files. Main-video height/scene-count options do not affect it.
`--short-seconds` accepts 45–60 and defaults to 55.

Actual narration length is checked. If outside the range, modest pitch-preserving
tempo adjustment (0.8–1.25×) is applied before caption alignment; otherwise the
script must be revised. Speech is never truncated to hit the limit. Final video
duration is checked and saved in `short-timing.json`. Reruns reuse matching work;
a changed source or duration target requires moving the old `short` folder.
To revise only Short narration, edit `short/script.txt` and remove its storyboard
before rerunning `short`. Generation uses the configured paid providers.

New storyboards default to a new image approximately every **6 seconds**, based
on script length at 150 words/minute (maximum 100 scenes). Actual timing follows
the narration. Use `--seconds-per-scene 4` for faster pacing, or `--scenes N`
to choose an exact count. The planner mixes detailed medium-quality scenes with
simpler low-quality cutaways in the same drawing style. Per-scene `image_quality`
is editable in the storyboard; older boards default to medium. Existing saved
storyboards retain their scene count: use a new output folder to replan.

Start with just an idea:

```sh
.venv/bin/python pipeline.py run --idea "What does IMAX mean?" --minutes 3 --scenes 6 --out runs/imax
```

OpenAI writes an original conversational script, then the pipeline creates the
storyboard, images, continuous RunPod narration, timed captions, and `video.mp4`.
`--minutes` is approximate (145 words/minute); actual speech duration varies.
The writer does not browse or independently fact-check. Include relevant facts
in your idea brief, or use `plan --idea ...` to review `script.txt` and
`storyboard.json` before generating assets. The original brief and generated
script are recorded in `idea.json`. Rerunning the same command reuses the script
and matching assets; use a new output directory for a different idea or length.
The same drawing style, character reference, 16:9 layout and punchy captions apply.

Or provide a finished script:

```sh
.venv/bin/python pipeline.py run --script examples/script.txt --scenes 3 --out runs/ocean
```

Continuous narration is the default: the entire script is sent in one TTS job.
Local Whisper word timestamps determine when each image appears, and the full
audio is attached once, without cuts or padding between scenes. Existing images
are reused when switching an old project to continuous narration. Run
`python pipeline.py run --out runs/ocean` to generate the new full narration and
rebuild. This makes one new billable narration request.

Alignment uses the installed `faster-whisper` dependency and its `small` model
on CPU. First use may download model weights; set `WHISPER_MODEL` to a compatible
local model directory for offline use. `transcript.json` records recognition,
and `alignment.json` caches matched script words and scene boundaries. These
timestamps are estimates, not guaranteed exact forced alignment; small unmatched
word spans are interpolated. If global word matching falls below 80%, a scene
falls below 65%, or boundaries are invalid, rendering stops for review. Editing
the narration or scene divisions invalidates alignment automatically.

Use `--narration-mode scenes` on both generation and rendering to retain the old
per-scene workflow. `render` defaults to continuous mode and requires
`assets/narration.wav` (or MP3); it does not silently concatenate old scene audio.

## Drawing style and recurring narrator

Every image request includes the **entire script** for context, the target scene's
narration, its visual description, and shared character/style instructions. The
default planner creates detailed 2D drawings with mostly still frames, occasional
gentle zooms to highlight details, and cuts.
It sets `show_character` per scene: the narrator can explain a scene while
environment/detail shots omit them. Edit that boolean and the scene prompt to
control appearances, clothes, expressions and poses. Existing storyboards retain
their saved style/motion until edited; `--style` only applies to new plans.

`characters/updated_base.png` contains your reference design. It is sent as an actual
image input to OpenAI's image edits endpoint for narrator scenes; it is not pasted
over the background. Other scenes use text-only generation. Override the PNG with
`--image-reference path/to/character.png` or `IMAGE_REFERENCE` in `.env`. Face and
proportions should stay recognizable while clothes and poses can change. Exact
character identity across generated images is not guaranteed.

Prompts are saved to `assets/scene-NNN.prompt.txt`. Changing the full script or
visual instructions invalidates affected image caches. Changing reference pixels
regenerates narrator images, while narration audio remains cached. On legacy
storyboards without `show_character`, the first and last scenes include the narrator.

Or use separate steps to review and edit the storyboard:

```sh
.venv/bin/python pipeline.py plan --script examples/script.txt --scenes 3 --out runs/ocean
# Edit runs/ocean/storyboard.json as desired.
.venv/bin/python pipeline.py assets --out runs/ocean
.venv/bin/python pipeline.py render --out runs/ocean
```

The planner must preserve your script's exact words and punctuation in order.
Use narration-only text: headings will also be spoken. `--scenes` is the requested
scene count, and `--style` sets the shared visual description. Landscape `16:9`
is the default for new plans; use `--aspect 9:16` for vertical videos. Rendering
reads the saved storyboard's aspect ratio and automatically selects 1920×1080
for landscape or 1080×1920 for portrait unless `--height` is supplied. To convert
an existing project, edit its storyboard's `aspect_ratio` and rerun `run` to
regenerate images for the new shape. This changes layout, not script length.

Render options:

```sh
.venv/bin/python pipeline.py render --out runs/ocean --height 360 --captions soft
.venv/bin/python pipeline.py render --out runs/ocean --music music.mp3 --music-volume 0.12
```

`--captions burn` is the default; `soft` adds a selectable subtitle track, and
`none` omits captions from the video. `captions.srt` is always exported.
Music loops to fill the video and is mixed at the specified linear volume (0–1).

## Outputs and resuming

- `storyboard.json`: title, style, aspect ratio, narration, visual prompts, camera,
  mood, estimated durations, motion, and transitions. Edit this to control scenes.
- `assets/`: per-scene PNG and WAV (RunPod) or MP3 (SiliconFlow), fingerprints,
  and saved RunPod job IDs.
- `clips/`: intermediate rendered scenes and decoded narration WAVs.
- `timeline.json`: measured audio durations and frame-aligned scene boundaries.
- `captions.srt`: timed phrases from the original narration.
- `video.mp4`: final video for human review.

Rerun `run` with the same output directory to resume. It uses the existing
storyboard; planning options only apply when creating one. A different supplied
script is rejected to avoid accidentally rendering an old storyboard.
`plan` never overwrites an existing storyboard. Use a new output directory for a
new project. After editing the storyboard, run `assets` again: unchanged requests
reuse existing files; changed narration regenerates its audio, and changed visual
prompts regenerate their images. Rendering always rebuilds the video. Do not run
two commands concurrently against the same output directory.

You can also place your own `scene-001.png` and `scene-001.mp3` or `scene-001.wav` files in `assets/` and
run `render` without any API keys. `render` deliberately uses those files as-is;
run `assets` first if you need them regenerated from edited prompts.

Narration is measured after decoding to avoid MP3 encoder padding. In continuous
mode, image boundaries snap to video frames while the audio stays intact, and
captions use matched speech timestamps. In legacy per-scene mode, each scene is
rounded up to a video frame and captions use approximate word-proportional timing.
Fade transitions do not overlap or truncate narration. Shared
style and descriptions encourage consistency but do not guarantee identical
characters between generated images. Images are center-cropped to the video
aspect ratio. There is no automatic upload or publication.

## Existing tools

`app.py` remains the standalone voice upload/list/delete/synthesis gateway:
`.venv/bin/uvicorn app:app --reload`. The pipeline calls the selected provider
directly, so running that API server is optional. `make_clip_ffmpeg.py` and
`srt_from_audio.py` remain available for the original character-overlay workflow.

## Verification

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Tests mock provider calls and render short real videos locally, including music,
zoom/fade, all caption modes, and paths with spaces/apostrophes. They do not spend
API credits. Live provider access requires your configured credentials.

While waiting for a provider, the CLI prints an elapsed-time update every 10
seconds. Storyboard requests use a 120-second read timeout; image and narration
requests use 300 seconds. These are network inactivity limits, not total job
deadlines. Ctrl+C cancels cleanly and preserves completed work. Rerun the same
command to resume. A cancelled or timed-out request may still finish at the
provider; it is not automatically retried.

Explicit temporary rate-limit errors (`rate_limit_exceeded`, `rate_limit_error`,
`slow_down`) retry up to three times, with 30/60/120-second backoff plus jitter,
honoring a longer `Retry-After`. Cooldowns print progress every ten seconds.
If the required delay exceeds five minutes, the CLI stops with instructions to
resume later. Billing/quota errors and ambiguous network failures are not retried.

RunPod uses `/run` and polls `/status/{job_id}` every five seconds, waiting up to
15 minutes locally. Ctrl+C or a polling timeout does **not** cancel the remote
job: rerun promptly to resume the saved ID without submitting again. Completed
async results are retained by RunPod for 30 minutes. If a submission response is
lost, the CLI stops rather than risking a duplicate paid job. Inspect Requests
in the RunPod console before removing the affected `scene-NNN.runpod-job.json`
file to allow a fresh submission. The same applies after resolving a failed or
expired job. Changing narration or reference audio regenerates its audio;
pending jobs with changed inputs must be resolved first.

RunPod API reference: [sending requests](https://docs.runpod.io/serverless/endpoints/send-requests).

API integration references: [structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
and [image generation](https://developers.openai.com/api/docs/guides/image-generation).
