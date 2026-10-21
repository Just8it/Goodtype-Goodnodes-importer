use base64::{Engine, engine::general_purpose::STANDARD};

use crate::{MIB, Result};

// SVG is displayed as an isolated image, but external resources/active XML are still forbidden.
pub fn validate(bytes: &[u8]) -> Result<()> {
    let text = std::str::from_utf8(bytes)?;
    let document = roxmltree::Document::parse(text)?;
    if document.root_element().tag_name().name() != "svg" {
        return Err("artwork is not SVG".into());
    }
    if document.descendants().count() > 200_000 {
        return Err("SVG element limit exceeded".into());
    }
    for node in document.descendants().filter(|n| n.is_element()) {
        if node.tag_name().namespace() != Some("http://www.w3.org/2000/svg")
            || !matches!(
                node.tag_name().name(),
                "svg"
                    | "g"
                    | "path"
                    | "rect"
                    | "circle"
                    | "ellipse"
                    | "line"
                    | "polyline"
                    | "polygon"
                    | "image"
            )
        {
            return Err("unsupported SVG element".into());
        }
        for attribute in node.attributes() {
            let value = attribute.value();
            if attribute
                .namespace()
                .is_some_and(|ns| ns != "http://www.w3.org/1999/xlink")
            {
                return Err("unsupported SVG attribute namespace".into());
            }
            match attribute.name() {
                "href" => {
                    let data = value
                        .strip_prefix("data:image/png;base64,")
                        .or_else(|| value.strip_prefix("data:image/jpeg;base64,"))
                        .ok_or("SVG may embed only PNG/JPEG data")?;
                    if data.len() > 28 * MIB {
                        return Err("embedded image too large".into());
                    }
                    validate_raster(&STANDARD.decode(data)?)?;
                }
                "fill" | "stroke" => {
                    if value != "none" && !goodtype_core::valid_hex_color(value) {
                        return Err("SVG paint must be a literal color".into());
                    }
                }
                "d" | "points" | "viewBox" | "x" | "y" | "x1" | "x2" | "y1" | "y2" | "width"
                | "height" | "rx" | "ry" | "cx" | "cy" | "r" | "stroke-width"
                | "stroke-opacity" | "fill-opacity" | "opacity" | "stroke-dasharray" => {
                    if !value.chars().all(|c| {
                        c.is_ascii_digit() || " .,+-eEMmLlHhVvCcSsQqTtAaZz\t\r\n".contains(c)
                    }) {
                        return Err("non-numeric SVG geometry".into());
                    }
                    // A textual exponent must not smuggle non-finite geometry into a renderer.
                    for token in value.split(|c: char| {
                        c.is_ascii_alphabetic() && c != 'e' && c != 'E'
                            || c.is_ascii_whitespace()
                            || c == ','
                    }) {
                        if let Ok(number) = token.parse::<f64>()
                            && (!number.is_finite() || number.abs() > 10_000_000.)
                        {
                            return Err("SVG coordinate exceeds bounds".into());
                        }
                    }
                }
                "transform" => {
                    let rest = value
                        .replace("rotate", "")
                        .replace("translate", "")
                        .replace("scale", "")
                        .replace("matrix", "");
                    if !rest
                        .chars()
                        .all(|c| c.is_ascii_digit() || " ().,+-eE".contains(c))
                    {
                        return Err("unsupported SVG transform".into());
                    }
                }
                "stroke-linecap" if matches!(value, "round" | "butt" | "square") => (),
                "stroke-linejoin" if matches!(value, "round" | "miter" | "bevel") => (),
                "fill-rule" if matches!(value, "nonzero" | "evenodd") => (),
                "preserveAspectRatio" if value == "none" => (),
                "overflow" if value == "hidden" => (),
                "class" | "data-stroke-id" => {
                    if value.len() > 128
                        || !value
                            .chars()
                            .all(|c| c.is_ascii_alphanumeric() || "-_ ".contains(c))
                    {
                        return Err("invalid SVG annotation".into());
                    }
                }
                _ => return Err(format!("unsupported SVG attribute: {}", attribute.name()).into()),
            }
        }
    }
    Ok(())
}

pub fn validate_raster(bytes: &[u8]) -> Result<()> {
    use image::{ImageFormat, ImageReader, Limits};
    use std::io::Cursor;
    if bytes.is_empty() || bytes.len() > 20 * MIB {
        return Err("image byte limit exceeded".into());
    }
    let format = image::guess_format(bytes)?;
    if !matches!(format, ImageFormat::Png | ImageFormat::Jpeg) {
        return Err("expected PNG or JPEG".into());
    }
    let (width, height) = ImageReader::with_format(Cursor::new(bytes), format).into_dimensions()?;
    if width == 0
        || height == 0
        || width > 32768
        || height > 32768
        || u64::from(width) * u64::from(height) > 100_000_000
    {
        return Err("image pixel limit exceeded".into());
    }
    let mut reader = ImageReader::with_format(Cursor::new(bytes), format);
    let mut limits = Limits::default();
    limits.max_image_width = Some(32768);
    limits.max_image_height = Some(32768);
    limits.max_alloc = Some(512 * MIB as u64);
    reader.limits(limits);
    reader.decode()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn artwork_rejects_active_and_external_content() {
        for body in [
            "<script/>",
            "<image href='https://example.com/a.png'/>",
            "<path fill='url(https://example.com/a)'/>",
            "<path style='fill:red'/>",
            "<g onload='alert(1)'/>",
            "<foreignObject/>",
            "<path d='M 1e999 0'/>",
        ] {
            let svg = format!("<svg xmlns='http://www.w3.org/2000/svg'>{body}</svg>");
            assert!(validate(svg.as_bytes()).is_err(), "{body}");
        }
        assert!(validate(b"<svg xmlns='http://www.w3.org/2000/svg'><path d='M 0 1 C 2 3 4 5 6 7 Z' fill='#000000'/></svg>").is_ok());
    }
    #[test]
    fn rejects_image_decompression_bomb_dimensions() {
        let mut png = b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR".to_vec();
        png.extend(u32::MAX.to_be_bytes());
        png.extend(100u32.to_be_bytes());
        assert!(validate_raster(&png).is_err());
    }
}
