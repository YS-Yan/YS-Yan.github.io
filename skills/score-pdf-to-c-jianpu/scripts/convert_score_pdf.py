from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from statistics import mean

try:
    import pdfplumber
    from pypdf import PdfReader, PdfWriter, Transformation
    from pypdf.generic import RectangleObject
    from reportlab.lib.colors import Color, HexColor
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfgen import canvas
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "Missing dependency. Install pdfplumber, pypdf, and reportlab before running."
    ) from exc


BLUE = HexColor("#075D7A")
PALE_BLUE = HexColor("#D5E7ED")
MID_GREY = HexColor("#76878E")
LIGHT_GREY = HexColor("#D8DFE2")


@dataclass(frozen=True)
class GlyphProfile:
    name: str
    note_chars: frozenset[str]
    rest_chars: frozenset[str]
    accidentals: dict[str, str]
    font_contains: str | None
    chord_x_tolerance: float
    accidental_y_tolerance: float
    accidental_gap: float
    pitch_residual_tolerance: float

    def accepts(self, char: dict) -> bool:
        return self.font_contains is None or self.font_contains in char.get("fontname", "")


PROFILES = {
    "maestro": GlyphProfile(
        name="maestro",
        note_chars=frozenset({"œ", "˙", "w"}),
        rest_chars=frozenset({"‰", "Œ", "Ó", "∑", "≈", "R"}),
        accidentals={"#": "#", "b": "b", "n": "n"},
        font_contains="Maestro",
        chord_x_tolerance=0.9,
        accidental_y_tolerance=0.85,
        accidental_gap=10.5,
        pitch_residual_tolerance=0.30,
    ),
    "microsoft-pua": GlyphProfile(
        name="microsoft-pua",
        note_chars=frozenset({"\uf0cf", "\uf021", "\uf077"}),
        rest_chars=frozenset({"\uf0ce", "\uf0b7"}),
        accidentals={"\uf062": "b", "\uf023": "#", "\uf06e": "n"},
        font_contains=None,
        chord_x_tolerance=0.95,
        accidental_y_tolerance=0.95,
        accidental_gap=10.5,
        pitch_residual_tolerance=0.52,
    ),
}


@dataclass
class Pitch:
    step: int
    degree: int
    octave: int
    accidental: str = ""


@dataclass
class Event:
    x: float
    pitches: list[Pitch] = field(default_factory=list)
    rest: bool = False


@dataclass
class System:
    page_index: int
    index_on_page: int
    staff_top: float
    staff_bottom: float
    staff_spacing: float
    crop_top: float
    crop_bottom: float
    events: list[Event]
    barlines: list[float]

    @property
    def max_chord(self) -> int:
        return max((len(event.pitches) for event in self.events), default=1)


@dataclass
class Panel:
    system: System
    panel_top: float
    panel_bottom: float
    source_top: float
    source_height: float
    ann_top: float
    ann_bottom: float
    scale_y: float


def select_profile(document: pdfplumber.pdf.PDF, requested: str) -> GlyphProfile:
    if requested != "auto":
        return PROFILES[requested]
    chars = [char for page in document.pages[: min(3, len(document.pages))] for char in page.chars]
    scores = {
        name: sum(
            char["text"] in profile.note_chars and profile.accepts(char) for char in chars
        )
        for name, profile in PROFILES.items()
    }
    name, score = max(scores.items(), key=lambda item: item[1])
    if score == 0:
        raise ValueError(
            "No supported music-font noteheads detected. Calibrate a new glyph profile first."
        )
    return PROFILES[name]


def detect_staff_groups(page: pdfplumber.page.Page) -> list[tuple[float, float, float]]:
    ys: list[float] = []
    for line in sorted(page.lines, key=lambda item: float(item["top"])):
        if float(line["x1"]) - float(line["x0"]) <= float(page.width) * 0.20:
            continue
        if float(line.get("linewidth", 1.0)) > 0.9:
            continue
        y = float(line["top"])
        if not ys or abs(y - ys[-1]) > 0.5:
            ys.append(y)

    groups: list[tuple[float, float, float]] = []
    index = 0
    while index <= len(ys) - 5:
        candidate = ys[index : index + 5]
        gaps = [candidate[i + 1] - candidate[i] for i in range(4)]
        average = sum(gaps) / 4
        if 3.5 < average < 7.5 and max(abs(gap - average) for gap in gaps) < 0.45:
            groups.append((candidate[0], candidate[-1], average))
            index += 5
        else:
            index += 1
    return groups


def pitch_from_y(y: float, staff_bottom: float, spacing: float, tolerance: float) -> Pitch:
    raw_step = (staff_bottom + spacing - y) / (spacing / 2)
    step = round(raw_step)
    residual = abs(raw_step - step)
    if residual > tolerance:
        raise ValueError(
            f"Uncertain pitch coordinate: y={y:.2f}, staff_bottom={staff_bottom:.2f}, "
            f"spacing={spacing:.2f}, residual={residual:.3f}."
        )
    octave, degree_index = divmod(step, 7)
    return Pitch(step=step, degree=degree_index + 1, octave=octave)


def pitch_from_char(
    char: dict, staff_bottom: float, spacing: float, profile: GlyphProfile
) -> Pitch:
    return pitch_from_y(
        float(char["top"]), staff_bottom, spacing, profile.pitch_residual_tolerance
    )


def collect_events(
    chars: list[dict],
    staff_bottom: float,
    spacing: float,
    barlines: list[float],
    profile: GlyphProfile,
    inherited_signature: dict[int, str],
) -> tuple[list[Event], dict[int, str]]:
    notes = [
        char for char in chars if char["text"] in profile.note_chars and profile.accepts(char)
    ]
    accidentals = [
        char for char in chars if char["text"] in profile.accidentals and profile.accepts(char)
    ]
    rests = [
        char for char in chars if char["text"] in profile.rest_chars and profile.accepts(char)
    ]

    note_groups: list[list[dict]] = []
    for note in sorted(notes, key=lambda item: (float(item["x0"]), float(item["top"]))):
        if (
            not note_groups
            or abs(float(note["x0"]) - mean(float(item["x0"]) for item in note_groups[-1]))
            > profile.chord_x_tolerance
        ):
            note_groups.append([note])
        else:
            note_groups[-1].append(note)

    events: list[Event] = []
    used_accidentals: set[int] = set()
    for group in note_groups:
        pitches: list[Pitch] = []
        for note in group:
            pitch = pitch_from_char(note, staff_bottom, spacing, profile)
            matches = [
                accidental
                for accidental in accidentals
                if 0 <= float(note["x0"]) - float(accidental["x1"]) <= profile.accidental_gap
                and abs(float(note["top"]) - float(accidental["top"]))
                <= profile.accidental_y_tolerance
            ]
            if matches:
                selected = max(matches, key=lambda item: float(item["x1"]))
                pitch.accidental = profile.accidentals[selected["text"]]
                used_accidentals.add(id(selected))
            pitches.append(pitch)
        x = mean((float(note["x0"]) + float(note["x1"])) / 2 for note in group)
        events.append(Event(x=x, pitches=sorted(pitches, key=lambda pitch: pitch.step, reverse=True)))

    rest_xs: list[float] = []
    for rest in sorted(rests, key=lambda item: float(item["x0"])):
        x = (float(rest["x0"]) + float(rest["x1"])) / 2
        if not rest_xs or abs(x - rest_xs[-1]) > 1.4:
            rest_xs.append(x)
            events.append(Event(x=x, rest=True))
    events.sort(key=lambda event: event.x)

    unused = [item for item in accidentals if id(item) not in used_accidentals]
    accidental_groups: list[list[dict]] = []
    for item in sorted(unused, key=lambda value: float(value["x0"])):
        if (
            not accidental_groups
            or float(item["x0"]) - max(float(value["x1"]) for value in accidental_groups[-1])
            > 10.0
        ):
            accidental_groups.append([item])
        else:
            accidental_groups[-1].append(item)

    signature_changes: list[tuple[float, dict[int, str]]] = []
    for group in accidental_groups:
        left = min(float(item["x0"]) for item in group)
        previous_barline = max(
            (barline for barline in barlines if barline <= left + 2.0), default=None
        )
        at_system_start = left < 130.0 and not any(
            event.pitches and event.x < left for event in events
        )
        after_barline = (
            previous_barline is not None
            and -2.0 <= left - previous_barline <= 28.0
            and not any(
                event.pitches and previous_barline + 1.0 < event.x < left for event in events
            )
        )
        if not (at_system_start or after_barline):
            continue
        replacement: dict[int, str] = {}
        for item in group:
            accidental = profile.accidentals[item["text"]]
            if accidental != "n":
                pitch_class = pitch_from_char(item, staff_bottom, spacing, profile).step % 7
                replacement[pitch_class] = accidental
        signature_changes.append((max(float(item["x1"]) for item in group), replacement))

    signature_changes.sort(key=lambda item: item[0])
    key_signature = dict(inherited_signature)
    active: dict[int, str] = {}
    bar_index = 0
    signature_index = 0
    for event in events:
        while signature_index < len(signature_changes) and event.x > signature_changes[signature_index][0]:
            key_signature = dict(signature_changes[signature_index][1])
            active.clear()
            signature_index += 1
        while bar_index < len(barlines) and event.x > barlines[bar_index] + 0.5:
            active.clear()
            bar_index += 1
        if event.rest:
            continue
        for pitch in event.pitches:
            if pitch.accidental == "n":
                active[pitch.step] = ""
                pitch.accidental = ""
            elif pitch.accidental:
                active[pitch.step] = pitch.accidental
            elif pitch.step in active:
                pitch.accidental = active[pitch.step]
            else:
                pitch.accidental = key_signature.get(pitch.step % 7, "")

    while signature_index < len(signature_changes):
        key_signature = dict(signature_changes[signature_index][1])
        signature_index += 1
    return events, key_signature


def extract_score(
    source: Path, requested_profile: str
) -> tuple[list[list[System]], GlyphProfile, list[tuple[float, float]]]:
    pages: list[list[System]] = []
    page_sizes: list[tuple[float, float]] = []
    inherited_signature: dict[int, str] = {}
    with pdfplumber.open(source) as document:
        profile = select_profile(document, requested_profile)
        for page_index, page in enumerate(document.pages):
            page_sizes.append((float(page.width), float(page.height)))
            groups = detect_staff_groups(page)
            if not groups:
                raise ValueError(f"No five-line staff systems detected on page {page_index + 1}.")
            centers = [(top + bottom) / 2 for top, bottom, _ in groups]
            systems: list[System] = []
            for index, (staff_top, staff_bottom, spacing) in enumerate(groups):
                if index == 0:
                    crop_top = max(0.0, staff_top - (55.0 if page_index == 0 else 70.0))
                else:
                    crop_top = (centers[index - 1] + centers[index]) / 2
                if index == len(groups) - 1:
                    crop_bottom = min(float(page.height), staff_bottom + 58.0)
                else:
                    crop_bottom = (centers[index] + centers[index + 1]) / 2

                band_chars = [
                    char for char in page.chars if crop_top <= float(char["top"]) < crop_bottom
                ]
                barlines = sorted(
                    set(
                        round(float(line["x0"]), 2)
                        for line in page.lines
                        if abs(float(line["x1"]) - float(line["x0"])) < 0.5
                        and abs(float(line["top"]) - staff_top) < 0.9
                        and abs(float(line["bottom"]) - staff_bottom) < 1.3
                    )
                )
                events, inherited_signature = collect_events(
                    band_chars,
                    staff_bottom,
                    spacing,
                    barlines,
                    profile,
                    inherited_signature,
                )
                systems.append(
                    System(
                        page_index,
                        index,
                        staff_top,
                        staff_bottom,
                        spacing,
                        crop_top,
                        crop_bottom,
                        events,
                        barlines,
                    )
                )
            pages.append(systems)
    return pages, profile, page_sizes


def validate_mapping(pages: list[list[System]], profile: GlyphProfile) -> None:
    expected = {0: (3, 0), 2: (5, 0), 4: (7, 0), 6: (2, 1), 8: (4, 1)}
    for systems in pages:
        for system in systems:
            for half_step, target in expected.items():
                pitch = pitch_from_y(
                    system.staff_bottom - half_step * system.staff_spacing / 2,
                    system.staff_bottom,
                    system.staff_spacing,
                    profile.pitch_residual_tolerance,
                )
                if (pitch.degree, pitch.octave) != target:
                    raise AssertionError(
                        f"Fixed-do mapping failed on page {system.page_index + 1}, "
                        f"system {system.index_on_page + 1}."
                    )


def make_panels(systems: list[System], page_height: float) -> tuple[list[Panel], float]:
    title_bottom = max(0.0, systems[0].staff_top - 55.0) if systems[0].page_index == 0 else 0.0
    boundaries = [title_bottom]
    for previous, current in zip(systems, systems[1:]):
        boundaries.append((previous.staff_bottom + current.staff_top) / 2)
    boundaries.append(page_height)

    panels: list[Panel] = []
    for index, system in enumerate(systems):
        panel_top = boundaries[index]
        panel_bottom = boundaries[index + 1]
        source_top = title_bottom if index == 0 else system.crop_top
        source_height = max(1.0, system.crop_bottom - source_top)
        annotation_height = max(16.0, min(36.0, 5.8 * system.max_chord + 6.0))
        available = panel_bottom - panel_top - annotation_height - 3.0
        scale_y = min(1.0, available / source_height)
        if scale_y <= 0:
            raise ValueError(
                f"Insufficient page space on page {system.page_index + 1}, "
                f"system {system.index_on_page + 1}."
            )
        ann_top = panel_top + source_height * scale_y + 1.0
        ann_bottom = min(panel_bottom - 2.0, ann_top + annotation_height)
        panels.append(
            Panel(
                system,
                panel_top,
                panel_bottom,
                source_top,
                source_height,
                ann_top,
                ann_bottom,
                scale_y,
            )
        )
    return panels, title_bottom


def largest_safe_font_size(system: System, height: float, maximum: float) -> float:
    events = [event for event in system.events if event.pitches]
    if not events:
        return min(7.6, maximum)
    vertical_limit = (height - 3.8) / max(1.0, system.max_chord + 0.25)
    horizontal_limit = maximum
    for left, right in zip(events, events[1:]):
        distance = right.x - left.x

        def width_at_one(event: Event) -> float:
            return max(
                (
                    pdfmetrics.stringWidth(
                        f"{pitch.accidental}{pitch.degree}", "Helvetica-Bold", 1.0
                    )
                    for pitch in event.pitches
                ),
                default=0.56,
            )

        combined = (width_at_one(left) + width_at_one(right)) / 2
        horizontal_limit = min(horizontal_limit, max(4.2, (distance - 0.65) / combined))
    return max(4.8, min(maximum, vertical_limit, horizontal_limit))


def draw_pitch(drawing, x: float, y_top: float, pitch: Pitch, size: float, page_h: float) -> None:
    label = f"{pitch.accidental}{pitch.degree}"
    drawing.setFillColor(BLUE)
    drawing.setFont("Helvetica-Bold", size)
    width = pdfmetrics.stringWidth(label, "Helvetica-Bold", size)
    baseline = page_h - y_top - size * 0.34
    drawing.drawString(x - width / 2, baseline, label)
    digit_width = pdfmetrics.stringWidth(str(pitch.degree), "Helvetica-Bold", size)
    digit_center = x + width / 2 - digit_width / 2
    radius = max(0.4, size * 0.075)
    for dot_index in range(abs(pitch.octave)):
        dot_y = (
            baseline + size * 0.98 + dot_index * 1.65
            if pitch.octave > 0
            else baseline - size * 0.38 - dot_index * 1.65
        )
        drawing.circle(digit_center, dot_y, radius, stroke=0, fill=1)


def draw_annotation(
    drawing, panel: Panel, page_w: float, page_h: float, max_font_size: float
) -> float:
    system = panel.system
    height = panel.ann_bottom - panel.ann_top
    # Leave inter-system rehearsal boxes in the far-left margin unobscured.
    margin = max(45.0, page_w * 0.065)
    drawing.setFillColor(Color(0.96, 0.982, 0.988, alpha=1))
    drawing.roundRect(
        margin,
        page_h - panel.ann_bottom,
        page_w - 2 * margin,
        height,
        2.0,
        stroke=0,
        fill=1,
    )
    drawing.setStrokeColor(PALE_BLUE)
    drawing.setLineWidth(0.3)
    for barline in system.barlines:
        if margin <= barline <= page_w - margin:
            drawing.line(
                barline,
                page_h - panel.ann_top - 1.5,
                barline,
                page_h - panel.ann_bottom + 1.5,
            )
    drawing.setFillColor(MID_GREY)
    drawing.setFont("Helvetica", 4.8)
    drawing.drawString(margin + 2.0, page_h - panel.ann_top - 5.8, "1=C")

    size = largest_safe_font_size(system, height, max_font_size)
    line_height = size + 0.45
    for event in system.events:
        if not margin <= event.x <= page_w - margin:
            continue
        if event.rest:
            drawing.setFillColor(BLUE)
            drawing.setFont("Helvetica-Bold", size)
            drawing.drawCentredString(
                event.x,
                page_h - (panel.ann_top + height / 2) - size * 0.3,
                "0",
            )
            continue
        total = len(event.pitches) * line_height
        first = panel.ann_top + (height - total) / 2 + line_height / 2
        for index, pitch in enumerate(event.pitches):
            draw_pitch(drawing, event.x, first + index * line_height, pitch, size, page_h)
    drawing.setStrokeColor(LIGHT_GREY)
    drawing.setLineWidth(0.25)
    drawing.line(
        margin,
        page_h - panel.panel_bottom + 0.5,
        page_w - margin,
        page_h - panel.panel_bottom + 0.5,
    )
    return size


def overlay_page(
    panels: list[Panel], page_w: float, page_h: float, max_font_size: float
) -> tuple[PdfReader, list[float]]:
    buffer = BytesIO()
    drawing = canvas.Canvas(buffer, pagesize=(page_w, page_h), pageCompression=1)
    sizes = [
        draw_annotation(drawing, panel, page_w, page_h, max_font_size) for panel in panels
    ]
    drawing.save()
    buffer.seek(0)
    return PdfReader(buffer), sizes


def build_pdf(
    source: Path,
    output: Path,
    pages: list[list[System]],
    page_sizes: list[tuple[float, float]],
    max_font_size: float,
) -> list[float]:
    source_reader = PdfReader(str(source))
    writer = PdfWriter()
    used_sizes: list[float] = []
    for page_index, systems in enumerate(pages):
        page_w, page_h = page_sizes[page_index]
        panels, title_bottom = make_panels(systems, page_h)
        target = writer.add_blank_page(width=page_w, height=page_h)

        if page_index == 0 and title_bottom > 0:
            title_source = deepcopy(source_reader.pages[0])
            source_h = float(title_source.mediabox.height)
            y0 = source_h - title_bottom
            trim = RectangleObject([0.0, y0, page_w, source_h])
            title_source.cropbox = trim
            title_source.trimbox = trim
            transform = Transformation().translate(ty=-y0).translate(ty=page_h - title_bottom)
            target.merge_transformed_page(title_source, transform, over=True, expand=False)

        for panel in panels:
            source_page = deepcopy(source_reader.pages[page_index])
            source_h = float(source_page.mediabox.height)
            y0 = source_h - (panel.source_top + panel.source_height)
            y1 = source_h - panel.source_top
            trim = RectangleObject([0.0, y0, page_w, y1])
            source_page.cropbox = trim
            source_page.trimbox = trim
            destination_bottom = page_h - (panel.panel_top + panel.source_height * panel.scale_y)
            transform = (
                Transformation()
                .translate(ty=-y0)
                .scale(sx=1.0, sy=panel.scale_y)
                .translate(ty=destination_bottom)
            )
            target.merge_transformed_page(source_page, transform, over=True, expand=False)

        overlay, sizes = overlay_page(panels, page_w, page_h, max_font_size)
        used_sizes.extend(sizes)
        target.merge_page(overlay.pages[0], over=True, expand=False)

    writer.add_metadata(
        {
            "/Title": f"{source.stem} - C-major fixed-do numbered notation",
            "/Subject": "1=C, 2=D, 3=E, 4=F, 5=G, 6=A, 7=B; original pagination preserved",
            "/Author": "Generated with score-pdf-to-c-jianpu",
        }
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as stream:
        writer.write(stream)
    return used_sizes


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Add fixed-do C-major numbered notation beneath staff systems in a PDF."
    )
    parser.add_argument("input", type=Path, help="Input score PDF")
    parser.add_argument("output", nargs="?", type=Path, help="Output annotated PDF")
    parser.add_argument(
        "--profile",
        choices=["auto", *PROFILES],
        default="auto",
        help="Music-font glyph profile",
    )
    parser.add_argument(
        "--max-font-size",
        type=float,
        default=8.2,
        help="Maximum adaptive numbered-notation font size in points",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    source = args.input.resolve()
    if not source.is_file():
        raise SystemExit(f"Input PDF not found: {source}")
    output = (
        args.output.resolve()
        if args.output
        else source.with_name(f"{source.stem}_C大调简谱.pdf")
    )
    pages, profile, page_sizes = extract_score(source, args.profile)
    validate_mapping(pages, profile)
    font_sizes = build_pdf(source, output, pages, page_sizes, max(4.8, args.max_font_size))

    systems = [system for page in pages for system in page]
    noteheads = sum(len(event.pitches) for system in systems for event in system.events)
    rests = sum(event.rest for system in systems for event in system.events)
    chords = sum(len(event.pitches) > 1 for system in systems for event in system.events)
    print(f"profile={profile.name}")
    print(f"pages={len(pages)}")
    print(f"systems={len(systems)}")
    print(f"systems_per_page={[len(page) for page in pages]}")
    print(f"noteheads={noteheads}")
    print(f"rests={rests}")
    print(f"chord_events={chords}")
    print(
        f"font_size_range={min(font_sizes):.2f}-{max(font_sizes):.2f}; "
        f"average={sum(font_sizes) / len(font_sizes):.2f}"
    )
    print(f"output={output}")


if __name__ == "__main__":
    main()

