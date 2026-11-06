# Session queue contract

[Back to the project README](../README.md)

Onoats writes one type-discriminated JSONL file per session under:

~~~text
sessions/pending/<session_id>.jsonl
~~~

This is the versioned interface for downstream consumers. The recorder moves a
completed file from `.active/` into `sessions/pending/` with an atomic
`rename(2)`, so consumers never see a partially published file.

## Line types

| Type | Required shape |
|---|---|
| `session_meta` | `{"type":"session_meta","run_id":"<hex>"}` plus optional `category` and `diarization`; always line 1 |
| `utterance` | `{"type":"utterance","time":...,"text":...,"source":"me"|"them"}` plus optional `speakers` and `overlap` |
| `silence_gap` | `{"type":"silence_gap","time":...,"duration_seconds":N}` |

## Source labels

`source` is the canonical frozen enum: `me` or `them`. Configurable
`[speakers]` names affect rendering only and never enter the queue.

Display labels such as `Them (S1)` are composed when rendering. They are not
stored in `text` or `source`.

## Session metadata

`session_meta` is the first non-blank line of every file.

### `run_id`

`run_id` is an opaque random token minted once per `onoats bot` process.
Every file rotated by one run shares the same token, including files produced by
silence timeout, Flush, `SIGUSR1`, or the menu bar. A new Start gets a new
token. Treat a missing token from an older file as unknown.

### `category`

A chosen `--category` appears on `session_meta`, not in the filename.
Onoats writes it only when a category is explicitly locked. It never writes a
default because consumers treat any non-blank value as a hard classification
lock.

The `<session_id>` filename stem remains the stable backfill key.

### `diarization`

When diarization is configured, the header can include:

~~~json
{"diarization":{"model":"<repo-id>","version":"<snapshot-revision>"}}
~~~

This records configuration provenance. It does not prove that every utterance
was labelled, and runtime degradation does not rewrite the header.

The header never lists speakers because they are unknown when the file is
created.

## Diarized utterances

A labelled them-channel utterance adds:

~~~json
{
  "type": "utterance",
  "source": "them",
  "text": "Example",
  "speakers": ["S1"],
  "overlap": false
}
~~~

For overlapping speech, `speakers` contains the two active labels with the
more active speaker first, and `overlap` is `true`.

Both fields are omitted when:

- diarization is off;
- the utterance remains unlabelled; or
- the source is `me`.

Treat absence as unlabelled. Labels are unique within one run, so
`(run_id, speaker_label)` identifies one diarization slot across all files
from that run. Labels are model slots, not human identities.

## Queue ownership

The recorder publishes to `sessions/pending/`. A consumer claims work by
moving it to `sessions/claimed/`, then finishes in `sessions/done/` or
`sessions/failed/`.

Only one consumer should drain a given queue. Do not run `onoats convert`
while another worker claims files from the same data root.

See [data location](configuration.md#data-location) for selecting and sharing a
queue root.
