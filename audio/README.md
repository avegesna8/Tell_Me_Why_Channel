# Audio library

Put new music in `audio/music/` and new effects in `audio/sfx/`, locally and in the
private bucket. Each GitHub run scans these bucket folders and downloads the
available audio. New MP3, WAV, FLAC, M4A and OGG files become candidates automatically;
there is no fixed list of sound IDs in the planner. Descriptive filenames help it
understand new sounds. Optional descriptions, selection tags, effect excerpts and
credits can be supplied in `library.json`. `library.runtime.json` is generated and
not committed. The checked-in catalog is authoritative for the initial seven files;
the older uploaded bucket catalog does not need replacing.

The library is configured for attribution-not-required tracks, as confirmed by
the owner. Keep using that filter for additions. For any attribution-required asset,
add its catalog entry with `attribution_required: true` and the exact credit in
`attribution_text`; selected credits are added to the video description.

Selection is optional and specific to the script. The planner can choose narration
only, music only, effects only, or both. Aim for zero to two meaningful effects per
minute, with a hard cap of about three and at least twelve seconds between cues.
Music stays quiet, ducks under narration and fades in/out. Effects use brief excerpts.
The siren is reserved for narration about alarms, warnings or emergencies.

Initial music mood tags are editing hints, not automated listening results; adjust
them after reviewing a mix. New files are presented by descriptive filename until
additional tags are supplied. One cached text-planning request per video selects
from the library; there are no music or sound-effect generation API calls. Adding
more library files does not change cached selections on an existing video.
