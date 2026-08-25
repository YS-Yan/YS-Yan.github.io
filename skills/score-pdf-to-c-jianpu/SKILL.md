---
name: score-pdf-to-c-jianpu
description: Convert a treble-clef single-staff part PDF into fixed-do C-major numbered notation beneath each staff system while preserving the original page count and visual correspondence. Use when a user asks for C调/C大调简谱 written under an existing five-line score; do not use for audio transcription or for changing the sounding key.
---

# Score PDF to C Jianpu

Produce an annotated PDF in which every staff system is followed by a large, aligned fixed-do numbered-notation row.

## Musical contract

- Treat “C大调简谱” as fixed pitch: `1=C, 2=D, 3=E, 4=F, 5=G, 6=A, 7=B`. Do not reinterpret `1` as the source key tonic and do not transpose the music.
- Apply printed key signatures and measure-scoped accidentals. Render flats and sharps as `b` and `#`; a natural sign cancels the active alteration for that staff position.
- Use dots above or below the degree for octave displacement.
- Stack simultaneous pitches vertically and write rests as `0`.
- Preserve the source page count. Reflow only inside each page so the added row does not cover the staff.

## Workflow

1. Inspect the source PDF and render representative pages. Confirm it is a text/vector, treble-clef, single-staff part. For scanned pages, other clefs, grand staff, or unsupported music fonts, calibrate or extend the extractor before converting.
2. Run the bundled converter:

   ```powershell
   python scripts/convert_score_pdf.py input.pdf output.pdf --profile auto --max-font-size 8.2
   ```

   `auto` supports the Finale/Maestro and Microsoft Print-to-PDF private-use glyph profiles documented in [references/mapping-and-qa.md](references/mapping-and-qa.md). Use `--profile maestro` or `--profile microsoft-pua` only when automatic detection is ambiguous.
3. Read the audit summary. Treat zero detected systems, zero noteheads, uncertain pitch coordinates, or a changed page count as failures.
4. Render every output page to PNG and visually compare it with the source. Check several low, middle, and high notes; every key change; dense chords; rests; and the final system. Read [references/mapping-and-qa.md](references/mapping-and-qa.md) when calibration or detailed QA is needed.
5. Increase `--max-font-size` only while the numbers remain inside their annotation bands and do not collide horizontally. Keep the adaptive minimum for dense passages rather than overlapping the staff.

## Delivery

Return the final PDF with a concise note stating that it uses fixed-do C-major mapping and preserves the source pagination. Mention any unsupported or manually interpreted passages instead of presenting them as certain.

