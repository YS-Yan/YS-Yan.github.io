# Mapping, calibration, and QA

## Contents

- Fixed-do pitch geometry
- Key signatures and accidentals
- Supported glyph profiles
- Layout and visual QA
- Unsupported inputs

## Fixed-do pitch geometry

For an ordinary treble staff, the bottom line is E4, which must print as `3` with no octave dot. Middle C is one staff spacing below the bottom line. If `s` is the distance between adjacent staff lines and `y` grows downward, use:

```text
middle_c_y = staff_bottom_y + s
diatonic_step = round((middle_c_y - notehead_center_y) / (s / 2))
octave, degree_index = divmod(diatonic_step, 7)
degree = degree_index + 1
```

Validate at least these landmarks before accepting a new PDF profile:

| Staff position | Fixed-do result |
|---|---|
| Bottom line E4 | `3` |
| Second line G4 | `5` |
| Third line B4 | `7` |
| Fourth line D5 | `2` with one upper dot |
| Fifth line F5 | `4` with one upper dot |

The PDF character box is font-dependent. Some fonts report a notehead-like `top`; others report a full glyph box. Calibrate against rendered pixels, not only extracted text coordinates. Reject a profile when the residual from the nearest half-space is too large.

## Key signatures and accidentals

- Identify a local accidental only when it sits immediately before a notehead at the same vertical position.
- An unused accidental group immediately after the clef or a barline is a key-signature change. Map its vertical position to a diatonic pitch class and apply it to later notes of that class.
- A local accidental carries to the next barline for the same staff position, including octave.
- A natural sign cancels the active alteration for that position until the next barline. Do not print `n` in the C-major row; print the unaltered degree.
- Key signatures carry across systems until another signature replaces them.

## Supported glyph profiles

The bundled script contains two profiles recovered by comparing extracted glyphs with rendered noteheads:

- `maestro`: Finale/Maestro noteheads `œ`, `˙`, and `w`; common Maestro rest and accidental characters.
- `microsoft-pua`: private-use glyphs emitted by Microsoft Print to PDF, including filled/open noteheads `U+F0CF`, `U+F021`, `U+F077`; rests `U+F0CE`, `U+F0B7`; flat/sharp/natural `U+F062`, `U+F023`, `U+F06E`.

Do not add a glyph merely because it appears frequently. Time signatures, common-time symbols, tremolo strokes, augmentation dots, and articulation marks can resemble rests or note components in extracted text. Confirm each new glyph at several coordinates on rendered pages.

## Layout and visual QA

Render every output page and verify:

1. Source and output page counts match.
2. Page headers, rehearsal marks, tempo changes, and final barlines remain on their original pages.
3. Every staff has one pale annotation band beneath it and no band covers staff notation.
4. Numbers align horizontally with their noteheads; chord tones are vertically stacked.
5. Sparse systems use the requested large type, while dense systems reduce type only enough to avoid collisions.
6. Key changes are visible in the numbered row from the first affected note.
7. Rests do not appear at time-signature or common-time glyph positions.

The output is an aligned pitch-reference row, not a fully re-engraved standalone jianpu score: it does not infer beams, duration underlines, ties, or every performance marking.

## Unsupported inputs

The script should not be trusted without extension for raster scans, bass/alto/tenor clefs, grand staff, tablature, percussion notation, handwritten music, or PDFs whose notation is outlines/images rather than extractable characters and lines. Use OMR or manual transcription and disclose that change of method.

