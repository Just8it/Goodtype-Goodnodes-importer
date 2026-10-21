use std::{
    error::Error,
    fs,
    io::Read,
    path::{Path, PathBuf},
};

use goodtype_core::{
    InkLayer, InkLayerReference, NotebookManifest, ObjectFields, Page, PageBackground,
    PageDefaults, PageGeometry, PageObject, PageReference, SCHEMA_VERSION, Stroke, StrokePattern,
    StrokePoint, StrokeTool, Transform, layout, paths, storage,
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

mod svg;

pub type Result<T> = std::result::Result<T, Box<dyn Error>>;
pub const DECODER_PIN: &str = "66028455fd7fc61b7931679915d811a25485e3ad";
pub const MIB: usize = 1024 * 1024;

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Decoded {
    pub version: u32,
    pub decoder: String,
    pub schema: String,
    pub pages: Vec<String>,
    pub warnings: Vec<String>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DecodedPage {
    pub width: f64,
    pub height: f64,
    pub background: Option<String>,
    pub pdf_page: u32,
    pub ink: Vec<DecodedStroke>,
    pub images: Vec<DecodedImage>,
    pub derived: Vec<Derived>,
    pub warnings: Vec<String>,
    pub source_uuid: String,
    pub source_strokes: usize,
    pub source_shapes: usize,
    pub source_images: usize,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DecodedStroke {
    pub color: String,
    pub opacity: f64,
    pub width: f64,
    pub highlighter: bool,
    pub pressure: bool,
    pub z: i32,
    pub points: Vec<[f64; 3]>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DecodedImage {
    pub file: String,
    pub x: f64,
    pub y: f64,
    pub width: f64,
    pub height: f64,
    pub z: i32,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Derived {
    pub file: String,
    pub z: i32,
    pub reason: String,
    pub source_count: usize,
}

#[derive(Debug, Serialize)]
pub struct PageReport {
    pub page: usize,
    pub source_uuid: String,
    pub source_strokes: usize,
    pub source_shapes: usize,
    pub source_images: usize,
    pub editable_strokes: usize,
    pub points: usize,
    pub original_images: usize,
    pub derived: Vec<Derived>,
    pub warnings: Vec<String>,
}

#[derive(Debug, Serialize)]
pub struct NotebookReport {
    pub source_sha256: String,
    pub decoder: String,
    pub schema: String,
    pub warnings: Vec<String>,
    pub pages: Vec<PageReport>,
}

pub fn read_limited(path: &Path, maximum: usize) -> Result<Vec<u8>> {
    let file = fs::File::open(path)?;
    if !file.metadata()?.is_file() || file.metadata()?.len() > maximum as u64 {
        return Err(format!("file exceeds {maximum} byte limit: {}", path.display()).into());
    }
    let mut bytes = Vec::new();
    file.take(maximum as u64 + 1).read_to_end(&mut bytes)?;
    if bytes.len() > maximum {
        return Err("file grew beyond byte limit".into());
    }
    Ok(bytes)
}

pub fn read_json<T: serde::de::DeserializeOwned>(path: &Path, maximum: usize) -> Result<T> {
    Ok(serde_json::from_slice(&read_limited(path, maximum)?)?)
}

fn scratch_file(root: &Path, name: &str) -> Result<PathBuf> {
    paths::validate_relative(name)?;
    if name.contains('/') || name.len() > 128 {
        return Err("intermediate files must have bounded flat names".into());
    }
    Ok(paths::contained(
        root,
        &root.join(name),
        "intermediate file",
    )?)
}

pub fn safe_name(value: &str) -> String {
    let mut name: String = value
        .chars()
        .map(|c| {
            if c.is_control() || "<>:\"/\\|?*".contains(c) {
                '_'
            } else {
                c
            }
        })
        .take(80)
        .collect();
    name = name.trim_matches([' ', '.']).to_owned();
    if name.is_empty() || paths::is_windows_reserved(&name) || name.starts_with('.') {
        name.insert_str(0, "Imported ");
    }
    name
}

/// Rust is the only canonical writer. The decoder directory is disposable interchange data.
pub fn convert(
    decoded_root: &Path,
    source: &Path,
    destination: &Path,
    title: &str,
    allow_derived: bool,
) -> Result<NotebookReport> {
    if destination.exists() {
        return Err("staging notebook already exists".into());
    }
    let decoded_root = paths::canonical_root(decoded_root)?;
    let decoded: Decoded = read_json(&decoded_root.join("decoded.json"), MIB)?;
    if decoded.version != 1
        || decoded.decoder != DECODER_PIN
        || !matches!(decoded.schema.as_str(), "0818" | "081f")
        || decoded.pages.is_empty()
        || decoded.pages.len() > 2000
    {
        return Err("unsupported or incomplete decoder contract".into());
    }
    let source_bytes = read_limited(source, 128 * MIB)?;
    let source_sha256 = format!("{:x}", Sha256::digest(&source_bytes));
    let timestamp =
        time::OffsetDateTime::now_utc().format(&time::format_description::well_known::Rfc3339)?;
    let mut report = NotebookReport {
        source_sha256,
        decoder: decoded.decoder,
        schema: decoded.schema,
        warnings: decoded.warnings,
        pages: Vec::new(),
    };
    for (index, filename) in decoded.pages.iter().enumerate() {
        let page: DecodedPage = read_json(&scratch_file(&decoded_root, filename)?, 64 * MIB)?;
        if !allow_derived && !page.derived.is_empty() {
            return Err(format!("page {} needs vector artwork ({}) — review and use --allow-derived to accept reduced editability",
                index+1, page.derived.iter().map(|v| v.reason.as_str()).collect::<Vec<_>>().join(", ")).into());
        }
        if page.ink.len() > 20_000
            || page.images.len() + page.derived.len() > 10_000
            || page.ink.iter().map(|s| s.points.len()).sum::<usize>() > 750_000
            || page.source_uuid.len() > 128
        {
            return Err("decoded page exceeds content limits".into());
        }
        let geometry = PageGeometry {
            width_pt: page.width,
            height_pt: page.height,
        };
        let background = match &page.background {
            Some(file) => {
                fs::create_dir_all(destination)?;
                let bytes =
                    read_limited(&scratch_file(&decoded_root, file)?, storage::MAX_PDF_BYTES)?;
                let source_path = storage::store_pdf_reference(destination, file, &bytes)?;
                PageBackground::Pdf {
                    source_path,
                    page: page.pdf_page,
                }
            }
            None => return Err("decoder did not supply a verified page background".into()),
        };
        let mut current = if index > 0 {
            storage::create_page(
                destination,
                &timestamp,
                &storage::PagePosition::Last,
                Some(&background),
                Some(&geometry),
            )?
        } else {
            let id = layout::page_id(1);
            storage::NotebookSnapshot {
                manifest: NotebookManifest {
                    schema_version: SCHEMA_VERSION,
                    id: uuid::Uuid::new_v4().to_string(),
                    title: title.into(),
                    pages: vec![PageReference {
                        id: id.clone(),
                        path: layout::page_path(&id),
                        geometry: geometry.clone(),
                    }],
                    default_page: PageDefaults {
                        geometry: geometry.clone(),
                        background: PageBackground::Plain {
                            color: "#ffffff".into(),
                        },
                    },
                    shared_style_path: None,
                    created_at: timestamp.clone(),
                    modified_at: timestamp.clone(),
                },
                page: Page {
                    schema_version: SCHEMA_VERSION,
                    id,
                    revision: 1,
                    geometry,
                    background,
                    objects: vec![],
                    reading_order: vec![],
                    ink_layers: vec![],
                },
                blocks: vec![],
                assets: vec![],
                ink_layers: vec![],
            }
        };
        let page_id = current.page.id.clone();
        let mut strokes = Vec::new();
        for (number, input) in page.ink.iter().enumerate() {
            if !goodtype_core::valid_hex_color(&input.color) {
                return Err("invalid decoded ink color".into());
            }
            strokes.push(Stroke {
                id: layout::stroke_id(&page_id, number + 1),
                z_index: input.z,
                tool: if input.highlighter {
                    StrokeTool::Highlighter
                } else {
                    StrokeTool::Pen
                },
                color: input.color.clone(),
                width_pt: input.width,
                pattern: StrokePattern::Solid,
                pattern_spacing: 1.,
                pressure: input.pressure,
                taper: 0.,
                opacity: input.opacity,
                group_id: None,
                points: input
                    .points
                    .iter()
                    .enumerate()
                    .map(|(n, p)| StrokePoint {
                        x: p[0],
                        y: p[1],
                        pressure: p[2],
                        time_ms: n as f64,
                        tilt_x: 0.,
                        tilt_y: 0.,
                    })
                    .collect(),
                transform: Transform {
                    translate_x: 0.,
                    translate_y: 0.,
                    scale_x: 1.,
                    scale_y: 1.,
                    rotation: 0.,
                },
            });
        }
        let layer_id = layout::ink_layer_id(&page_id, 1);
        current.page.ink_layers = vec![InkLayerReference {
            id: layer_id.clone(),
            path: layout::ink_layer_path(&page_id, 1),
        }];
        current.ink_layers = vec![InkLayer {
            schema_version: SCHEMA_VERSION,
            id: layer_id,
            page_id: page_id.clone(),
            strokes,
        }];
        current.page.objects.clear();
        current.page.reading_order.clear();
        for image in &page.images {
            let bytes = read_limited(
                &scratch_file(&decoded_root, &image.file)?,
                storage::MAX_IMAGE_BYTES,
            )?;
            svg::validate_raster(&bytes)?;
            let path = storage::store_pasted_image(destination, &image.file, &bytes)?;
            add_image(
                &mut current,
                &timestamp,
                image,
                path,
                "Imported Goodnotes image",
            );
        }
        for artwork in &page.derived {
            let bytes = read_limited(
                &scratch_file(&decoded_root, &artwork.file)?,
                storage::MAX_IMAGE_BYTES,
            )?;
            svg::validate(&bytes)?;
            let path = storage::store_pasted_image(destination, &artwork.file, &bytes)?;
            let image = DecodedImage {
                file: artwork.file.clone(),
                x: 0.,
                y: 0.,
                width: page.width,
                height: page.height,
                z: artwork.z,
            };
            add_image(
                &mut current,
                &timestamp,
                &image,
                path,
                &format!("Goodnotes vector artwork: {}", artwork.reason),
            );
        }
        if index == 0 {
            storage::create_notebook(destination, &current)?;
        } else {
            storage::save_notebook(destination, &current)?;
        }
        report.pages.push(PageReport {
            page: index + 1,
            source_uuid: page.source_uuid,
            source_strokes: page.source_strokes,
            source_shapes: page.source_shapes,
            source_images: page.source_images,
            editable_strokes: page.ink.len(),
            points: page.ink.iter().map(|s| s.points.len()).sum(),
            original_images: page.images.len(),
            derived: page.derived,
            warnings: page.warnings,
        });
    }
    // Every original attachment, including cropped image bytes, remains recoverable offline.
    fs::create_dir(destination.join("originals"))?;
    storage::files::persist_atomic(
        &destination.join("originals/source.goodnotes"),
        &source_bytes,
        false,
    )?;
    storage::files::persist_atomic(
        &destination.join("import-report.json"),
        &serde_json::to_vec_pretty(&report)?,
        false,
    )?;
    let reopened = storage::open_notebook(destination)?;
    for reference in &reopened.manifest.pages {
        storage::open_page(destination, &reference.id)?;
    }
    Ok(report)
}

fn add_image(
    snapshot: &mut storage::NotebookSnapshot,
    timestamp: &str,
    image: &DecodedImage,
    source_path: String,
    alt: &str,
) {
    let number = snapshot.page.objects.len();
    let id = layout::object_id(&snapshot.page.id, number + 1);
    snapshot.page.reading_order.push(id.clone());
    snapshot.page.objects.push(PageObject::Image {
        fields: ObjectFields {
            id,
            x: image.x,
            y: image.y,
            rotation: 0.,
            scale: 1.,
            z_index: image.z,
            reading_order: number as u32,
            group_id: None,
            created_at: timestamp.into(),
            modified_at: timestamp.into(),
        },
        source_path,
        width_pt: image.width,
        height_pt: image.height,
        alt_text: alt.into(),
    });
}

/// Reserve a new notebook directory and publish its manifest last. No replacement operation.
pub fn publish(source: &Path, destination: &Path) -> Result<()> {
    fs::create_dir(destination)?;
    let root = paths::canonical_root(source)?;
    copy_tree(&root, &root, destination)?;
    Ok(())
}

fn copy_tree(root: &Path, source: &Path, destination: &Path) -> Result<()> {
    let mut entries = fs::read_dir(source)?.collect::<std::io::Result<Vec<_>>>()?;
    entries.sort_by_key(|e| e.file_name() == layout::MANIFEST);
    for entry in entries {
        if entry.file_type()?.is_symlink() {
            return Err("symlink in staged notebook".into());
        }
        let path = paths::contained(root, &entry.path(), "publication source")?;
        let target = destination.join(entry.file_name());
        if entry.file_type()?.is_dir() {
            fs::create_dir(&target)?;
            copy_tree(root, &path, &target)?;
        } else {
            storage::files::persist_atomic(&target, &read_limited(&path, 128 * MIB)?, false)?;
        }
    }
    Ok(())
}
