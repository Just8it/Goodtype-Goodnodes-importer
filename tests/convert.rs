use std::{fs, path::Path};

use goodtype_core::{PageObject, storage};
use goodtype_goodnotes_import::{DECODER_PIN, convert, publish, safe_name};
use serde_json::json;

fn fixture(root: &Path, derived: bool) {
    fs::create_dir(root).unwrap();
    fs::write(
        root.join("background.pdf"),
        b"%PDF-1.4\nsynthetic storage fixture",
    )
    .unwrap();
    fs::write(root.join("art.svg"), b"<svg xmlns='http://www.w3.org/2000/svg' width='100' height='200'><path d='M 1 2 L 3 4' stroke='#123456' fill='none'/></svg>").unwrap();
    let page = json!({"width":100.,"height":200.,"background":"background.pdf","pdf_page":1,
        "source_uuid":"source-page","source_strokes":1,"source_shapes":if derived {1} else {0},"source_images":0,
        "ink":[{"color":"#123456","opacity":1.,"width":2.,"highlighter":false,"pressure":true,"z":0,
            "points":[[10.,20.,0.],[11.,21.,1.]]}],"images":[],"shapes":[],"warnings":[],
        "derived":if derived {json!([{"file":"art.svg","z":1,"x":12.,"y":24.,"width":8.,"height":9.,"reason":"native outline ink","source_count":1}])} else {json!([])}});
    fs::write(root.join("page-1.json"), serde_json::to_vec(&page).unwrap()).unwrap();
    fs::write(root.join("page-2.json"), serde_json::to_vec(&page).unwrap()).unwrap();
    fs::write(root.join("decoded.json"), serde_json::to_vec(&json!({"version":2,"decoder":DECODER_PIN,"schema":"0818","pages":["page-1.json","page-2.json"],"warnings":[]})).unwrap()).unwrap();
}

#[test]
fn output_is_an_independent_editable_movable_notebook() {
    let temp = tempfile::tempdir().unwrap();
    let decoded = temp.path().join("decoded");
    fixture(&decoded, true);
    let source = temp.path().join("source.goodnotes");
    fs::write(&source, b"private original placeholder").unwrap();
    let staged = temp.path().join("staged");
    let report = convert(&decoded, &source, &staged, "Lecture", true).unwrap();
    assert_eq!(report.pages.len(), 2);
    assert_eq!(report.pages[0].editable_strokes, 1);
    let published = temp.path().join("published");
    publish(&staged, &published).unwrap();
    assert!(publish(&staged, &published).is_err());
    // Lose the decoder and source: all canonical content must still work.
    fs::remove_file(&source).unwrap();
    let mut snapshot = storage::open_notebook(&published).unwrap();
    assert_eq!(snapshot.manifest.pages.len(), 2);
    assert_eq!(snapshot.ink_layers[0].strokes[0].points[0].pressure, 0.);
    let PageObject::Image {
        fields,
        width_pt,
        height_pt,
        ..
    } = &snapshot.page.objects[0]
    else {
        panic!("expected cropped vector image");
    };
    assert_eq!(
        (fields.x, fields.y, *width_pt, *height_pt),
        (12., 24., 8., 9.)
    );
    snapshot.ink_layers[0].strokes[0].points[0].x = 42.;
    snapshot.page.revision += 1;
    storage::save_notebook(&published, &snapshot).unwrap();
    let moved = temp.path().join("sorted-lecture");
    fs::rename(&published, &moved).unwrap();
    let reopened = storage::open_notebook(&moved).unwrap();
    assert_eq!(reopened.ink_layers[0].strokes[0].points[0].x, 42.);
    storage::open_page(&moved, &reopened.manifest.pages[1].id).unwrap();
    assert_eq!(
        fs::read(moved.join("originals/source.goodnotes")).unwrap(),
        b"private original placeholder"
    );
}

#[test]
fn fallback_requires_opt_in_and_failure_never_publishes() {
    let temp = tempfile::tempdir().unwrap();
    let decoded = temp.path().join("decoded");
    fixture(&decoded, true);
    let source = temp.path().join("source.goodnotes");
    fs::write(&source, b"original").unwrap();
    let staged = temp.path().join("staged");
    assert!(convert(&decoded, &source, &staged, "Lecture", false).is_err());
    assert!(!staged.join("goodtype.json").exists());
    fs::write(
        decoded.join("art.svg"),
        b"<svg xmlns='http://www.w3.org/2000/svg'><script/></svg>",
    )
    .unwrap();
    assert!(convert(&decoded, &source, &staged, "Lecture", true).is_err());
    assert!(!staged.join("goodtype.json").exists());
}

#[test]
fn intermediate_paths_and_portable_names_are_checked() {
    for name in ["CON", "../outside", "a:b", "  .  ", "<bad>"] {
        let safe = safe_name(name);
        assert!(!safe.contains(['/', '\\', ':', '<', '>']));
        assert!(!safe.is_empty());
        assert!(!goodtype_core::paths::is_windows_reserved(&safe));
    }
    let temp = tempfile::tempdir().unwrap();
    let decoded = temp.path().join("decoded");
    fixture(&decoded, false);
    let source = temp.path().join("source.goodnotes");
    fs::write(&source, b"original").unwrap();
    let mut contract: serde_json::Value =
        serde_json::from_slice(&fs::read(decoded.join("decoded.json")).unwrap()).unwrap();
    contract["pages"] = json!(["../outside.json"]);
    fs::write(
        decoded.join("decoded.json"),
        serde_json::to_vec(&contract).unwrap(),
    )
    .unwrap();
    assert!(
        convert(
            &decoded,
            &source,
            &temp.path().join("staged"),
            "Lecture",
            false
        )
        .is_err()
    );
}

#[test]
fn imported_shapes_use_canonical_geometry_and_can_be_edited_after_moving() {
    let temp = tempfile::tempdir().unwrap();
    let decoded = temp.path().join("decoded");
    fixture(&decoded, false);
    let path = decoded.join("page-1.json");
    let mut page: serde_json::Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
    page["shapes"] = json!([{
        "x": 12., "y": 24., "rotation": 30., "z": 1,
        "geometry": {"kind": "rectangle", "widthPt": 40., "heightPt": 20., "cornerRadiusPt": 2.},
        "style": {"strokeColor": "#123456", "strokeWidthPt": 2., "fillColor": null, "opacity": 1.}
    }]);
    page["source_shapes"] = json!(1);
    fs::write(&path, serde_json::to_vec(&page).unwrap()).unwrap();
    let source = temp.path().join("source.goodnotes");
    fs::write(&source, b"original").unwrap();
    let staged = temp.path().join("staged");
    let report = convert(&decoded, &source, &staged, "Lecture", false).unwrap();
    assert_eq!(report.pages[0].editable_shapes, 1);
    assert!(report.pages[0].derived.is_empty());
    let moved = temp.path().join("moved");
    publish(&staged, &moved).unwrap();
    let mut snapshot = storage::open_notebook(&moved).unwrap();
    let PageObject::Shape {
        fields,
        geometry,
        style,
    } = &mut snapshot.page.objects[0]
    else {
        panic!("expected native editable shape");
    };
    assert_eq!(
        (fields.x, fields.y, fields.rotation, fields.z_index),
        (12., 24., 30., 1)
    );
    *geometry = goodtype_core::ShapeGeometry::Line {
        start: goodtype_core::ShapePoint { x: 0., y: 0. },
        end: goodtype_core::ShapePoint { x: 42., y: 20. },
    };
    style.stroke_color = "#abcdef".into();
    snapshot.page.revision += 1;
    storage::save_notebook(&moved, &snapshot).unwrap();
    let reopened = storage::open_notebook(&moved).unwrap();
    assert!(
        matches!(&reopened.page.objects[0], PageObject::Shape { geometry: goodtype_core::ShapeGeometry::Line { end, .. }, style, .. }
        if end.x == 42. && style.stroke_color == "#abcdef")
    );

    page["shapes"][0]["geometry"]["widthPt"] = json!(-1.);
    fs::write(&path, serde_json::to_vec(&page).unwrap()).unwrap();
    assert!(
        convert(
            &decoded,
            &source,
            &temp.path().join("invalid"),
            "Bad",
            false
        )
        .is_err()
    );
}

#[test]
fn invalid_artwork_placement_never_publishes() {
    let temp = tempfile::tempdir().unwrap();
    let decoded = temp.path().join("decoded");
    fixture(&decoded, true);
    let path = decoded.join("page-1.json");
    let mut page: serde_json::Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
    page["derived"][0]["width"] = json!(-8.);
    fs::write(&path, serde_json::to_vec(&page).unwrap()).unwrap();
    let source = temp.path().join("source.goodnotes");
    fs::write(&source, b"original").unwrap();
    let staged = temp.path().join("staged");
    assert!(convert(&decoded, &source, &staged, "Bad", true).is_err());
    assert!(!staged.join("goodtype.json").exists());
}
