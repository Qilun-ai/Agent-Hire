//! Single-page flow in millimetres and AFM-based line wrapping.

use pdf_writer::{Content, Name, Str};

use crate::helvetica;
use crate::markdown::Face;

pub(crate) const POINTS_PER_MM: f64 = 72.0 / 25.4;
pub(crate) type Color = [u8; 3];

#[derive(Clone, Copy)]
pub(crate) struct Style {
    pub(crate) face: Face,
    pub(crate) size: f64,
    pub(crate) height: f64,
    pub(crate) color: Color,
}

#[derive(Clone, Copy)]
pub(crate) enum Align {
    Left,
    Center,
    Right,
}

#[derive(Clone, Copy)]
struct Glyph {
    code: u8,
    face: Face,
}

pub(crate) struct Layout {
    pub(crate) width: f64,
    pub(crate) height: f64,
    pub(crate) margin: f64,
    pub(crate) top: f64,
    pub(crate) y: f64,
    pub(crate) failed: bool,
    pub(crate) content: Content,
}

impl Layout {
    pub(crate) fn new(width: f64, height: f64, margin: f64, top: f64) -> Self {
        Self {
            width,
            height,
            margin,
            top,
            y: top,
            failed: false,
            content: Content::new(),
        }
    }

    pub(crate) fn usable(&self) -> f64 {
        self.width - 2.0 * self.margin
    }

    pub(crate) fn ratio(&self) -> f64 {
        (self.y - self.top) / (self.height - 2.0 * self.top).max(1.0)
    }

    pub(crate) fn fit(&mut self, height: f64) -> bool {
        if self.y + height > self.height - self.top {
            self.failed = true;
        }
        !self.failed
    }

    pub(crate) fn rule(&mut self, inset: f64, stroke: f64, color: Color) {
        let [red, green, blue] = rgb(color);
        let y = points(self.height - self.y);
        self.content
            .set_stroke_rgb(red, green, blue)
            .set_line_width(points(stroke))
            .move_to(points(self.margin + inset), y)
            .line_to(points(self.width - self.margin - inset), y)
            .stroke();
    }

    pub(crate) fn text(&mut self, text: &str, indent: f64, width: f64, style: Style, align: Align) {
        let glyphs = glyphs(text, style.face);
        self.draw(&glyphs, indent, width, style, align);
    }

    pub(crate) fn styled(
        &mut self,
        runs: &[(String, Face)],
        indent: f64,
        width: f64,
        style: Style,
    ) {
        let glyphs = runs
            .iter()
            .flat_map(|(text, face)| glyphs(text, *face))
            .collect::<Vec<_>>();
        self.draw(&glyphs, indent, width, style, Align::Left);
    }

    fn draw(&mut self, glyphs: &[Glyph], indent: f64, width: f64, style: Style, align: Align) {
        let [red, green, blue] = rgb(style.color);
        for line in wrap(glyphs, width, style.size) {
            let line_width = line
                .iter()
                .map(|glyph| advance(*glyph, style.size))
                .sum::<f64>();
            let offset = match align {
                Align::Left => 0.0,
                Align::Center => (width - line_width) / 2.0,
                Align::Right => width - line_width,
            }
            .max(0.0);
            let mut x = self.margin + indent + offset;
            let baseline = self.y + 0.5 * style.height + 0.3 * style.size / POINTS_PER_MM;
            for run in line.chunk_by(|left, right| left.face == right.face) {
                let bytes = run.iter().map(|glyph| glyph.code).collect::<Vec<_>>();
                let font = match run[0].face {
                    Face::Regular => Name(b"F1"),
                    Face::Bold => Name(b"F2"),
                    Face::Oblique => Name(b"F3"),
                };
                self.content
                    .set_fill_rgb(red, green, blue)
                    .begin_text()
                    .set_font(font, style.size as f32)
                    .set_text_matrix([
                        1.0,
                        0.0,
                        0.0,
                        1.0,
                        points(x),
                        points(self.height - baseline),
                    ])
                    .show(Str(&bytes))
                    .end_text();
                x += run
                    .iter()
                    .map(|glyph| advance(*glyph, style.size))
                    .sum::<f64>();
            }
            self.y += style.height;
        }
    }
}

fn rgb(color: Color) -> [f32; 3] {
    color.map(|channel| f32::from(channel) / 255.0)
}

pub(crate) fn points(mm: f64) -> f32 {
    (mm * POINTS_PER_MM) as f32
}

pub(crate) fn measure(text: &str, style: Style) -> f64 {
    glyphs(text, style.face)
        .into_iter()
        .map(|glyph| advance(glyph, style.size))
        .sum()
}

fn glyphs(text: &str, face: Face) -> Vec<Glyph> {
    text.chars()
        .filter_map(|character| {
            let code = match character {
                '\t' => b' ',
                ' '..='~' | '\u{a0}'..='\u{ff}' => character as u8,
                _ => return None,
            };
            Some(Glyph { code, face })
        })
        .collect()
}

fn advance(glyph: Glyph, size: f64) -> f64 {
    let table = match glyph.face {
        Face::Bold => &helvetica::BOLD,
        Face::Regular | Face::Oblique => &helvetica::REGULAR,
    };
    f64::from(table[usize::from(glyph.code)]) * size / (1000.0 * POINTS_PER_MM)
}

fn wrap(glyphs: &[Glyph], width: f64, size: f64) -> Vec<&[Glyph]> {
    let mut current = glyphs;
    let mut lines = Vec::new();
    while !current.is_empty() {
        let mut used = 0.0;
        let mut space = None;
        let mut end = current.len();
        for (index, glyph) in current.iter().enumerate() {
            used += advance(*glyph, size);
            if used > width && index != 0 {
                end = space.filter(|position| *position > 0).unwrap_or(index);
                break;
            }
            if glyph.code == b' ' {
                space = Some(index);
            }
        }
        let (line, rest) = current.split_at(end);
        let trimmed = line
            .iter()
            .rposition(|glyph| glyph.code != b' ')
            .map_or(0, |end| end + 1);
        lines.push(&line[..trimmed]);
        current = rest.trim_ascii_start_glyphs();
    }
    if lines.is_empty() {
        lines.push(&[]);
    }
    lines
}

trait GlyphTrim {
    fn trim_ascii_start_glyphs(&self) -> &Self;
}

impl GlyphTrim for [Glyph] {
    fn trim_ascii_start_glyphs(&self) -> &Self {
        let start = self
            .iter()
            .position(|glyph| glyph.code != b' ')
            .unwrap_or(self.len());
        &self[start..]
    }
}
