use std::{
    collections::{HashMap, HashSet},
    env, fs,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    thread,
    time::{Duration, Instant},
};

use goodtype_core::{paths, storage::files::persist_atomic};
use goodtype_goodnotes_import::{
    MIB, Result, convert, publish, read_json, read_limited, safe_name,
};
use serde::{Deserialize, Serialize};

mod process_limit;
mod runtime;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Item {
    name: String,
    folders: Vec<String>,
    source: PathBuf,
    original_path: Option<String>,
}

#[derive(Serialize)]
struct Outcome {
    status: &'static str,
    source: String,
    target: Option<String>,
    pages: usize,
    error: Option<String>,
}

fn main() {
    if let Err(error) = run() {
        eprintln!("Goodnotes import: {error}");
        std::process::exit(1);
    }
}

fn run() -> Result<()> {
    let arguments: Vec<_> = env::args_os().skip(1).collect();
    if arguments.first().is_some_and(|arg| arg == "--version") {
        if arguments.len() == 2 && arguments[1] == "--json" {
            println!(
                "{}",
                serde_json::json!({"name":"goodtype-goodnotes-import", "version":env!("CARGO_PKG_VERSION"), "protocolVersion":1, "notebookSchemaVersion":goodtype_core::SCHEMA_VERSION, "decoderCommit":goodtype_goodnotes_import::DECODER_PIN})
            );
        } else if arguments.len() == 1 {
            println!("Goodtype Goodnotes Importer {}", env!("CARGO_PKG_VERSION"));
        } else {
            return Err("usage: goodtype-goodnotes-import --version [--json]".into());
        }
        return Ok(());
    }
    if arguments.len() == 1 && arguments[0] == "--check" {
        let runtime = runtime::locate()?;
        return decoder(&runtime, "check", Path::new("-"), Path::new("-"));
    }
    if arguments.len() == 1 && (arguments[0] == "--help" || arguments[0] == "-h") {
        println!(
            "Goodtype Goodnotes Importer\n\nUsage: goodtype-goodnotes-import <source.goodnotes|backup.zip> <NEW destination folder> [--allow-derived] [--title <title>]\n\n--allow-derived  Accept vector artwork with reduced editability\n--version --json Print the version and compatibility contract\n--check          Verify the bundled decoder installation\n\nConversion stays local. Original exports are preserved in each notebook."
        );
        return Ok(());
    }
    let mut args = env::args_os().skip(1);
    let usage = "usage: goodtype-goodnotes-import <source.goodnotes|backup.zip> <NEW destination folder> [--allow-derived] [--title <single notebook title>]";
    let Some(source) = args.next() else {
        return Err(usage.into());
    };
    let Some(destination) = args.next() else {
        return Err(usage.into());
    };
    let source = fs::canonicalize(source)?;
    let destination = PathBuf::from(destination);
    let mut allow_derived = false;
    let mut title = None;
    while let Some(argument) = args.next() {
        match argument.to_str() {
            Some("--allow-derived") => allow_derived = true,
            Some("--title") => {
                title = Some(
                    args.next()
                        .ok_or(usage)?
                        .into_string()
                        .map_err(|_| "title is not Unicode")?,
                )
            }
            _ => return Err(usage.into()),
        }
    }
    if destination.try_exists()? {
        return Err("destination already exists; choose a new batch folder".into());
    }
    let parent = paths::canonical_root(
        destination
            .parent()
            .filter(|p| !p.as_os_str().is_empty())
            .unwrap_or(Path::new(".")),
    )?;
    let name = destination
        .file_name()
        .ok_or("destination needs a folder name")?;
    let destination = parent.join(name);
    if !matches!(
        source
            .extension()
            .and_then(|s| s.to_str())
            .map(str::to_ascii_lowercase)
            .as_deref(),
        Some("goodnotes" | "zip")
    ) {
        return Err("select a .goodnotes file or a backup .zip".into());
    }
    let runtime = runtime::locate()?;
    let scratch = tempfile::Builder::new()
        .prefix(".goodnotes-import-")
        .tempdir_in(&parent)?;
    let inventory = scratch.path().join("inventory");
    decoder(&runtime, "inventory", &source, &inventory)?;
    let items: Vec<Item> = read_json(&inventory.join("inventory.json"), MIB)?;
    if items.is_empty() || items.len() > 500 || (title.is_some() && items.len() != 1) {
        return Err("expected 1–500 notebooks; --title is only for a single notebook".into());
    }
    // Resolve the complete inventory before creating the destination or converting anything.
    let mut targets = HashSet::new();
    let mut folder_names = HashMap::new();
    let mut jobs = Vec::new();
    for (index, item) in items.into_iter().enumerate() {
        let item_source = fs::canonicalize(&item.source)?;
        if item_source != source {
            paths::contained(
                &paths::canonical_root(&inventory)?,
                &item_source,
                "backup notebook",
            )?;
        }
        if item.folders.len() > 12 {
            return Err("backup folder nesting exceeds 12".into());
        }
        let mut relative = PathBuf::new();
        for folder in &item.folders {
            let safe = safe_name(folder);
            relative.push(&safe);
            let key = relative.to_string_lossy().to_lowercase();
            if folder_names
                .insert(key, folder.clone())
                .is_some_and(|old| old != *folder)
            {
                return Err("backup folder names collide after portable-name normalization".into());
            }
        }
        let notebook_title = title.as_ref().unwrap_or(&item.name).clone();
        let stem = safe_name(&notebook_title);
        let mut candidate = relative.join(&stem);
        if !targets.insert(candidate.to_string_lossy().to_lowercase()) {
            candidate = relative.join(format!("{stem} ({})", index + 1));
            if !targets.insert(candidate.to_string_lossy().to_lowercase()) {
                return Err("backup notebook names collide".into());
            }
        }
        jobs.push((
            item_source,
            notebook_title,
            candidate,
            item.original_path.unwrap_or(item.name),
        ));
    }
    for (_, _, relative, _) in &jobs {
        let key = relative.to_string_lossy().to_lowercase();
        if folder_names.contains_key(&key) || key == "import-report.json" {
            return Err("backup notebook and folder/report names collide".into());
        }
    }
    fs::create_dir(&destination)?;
    let destination_root = paths::canonical_root(&destination)?;
    let mut outcomes: Vec<_> = jobs
        .iter()
        .map(|(_, _, relative, original)| Outcome {
            status: "pending",
            source: original.clone(),
            target: Some(relative.to_string_lossy().replace('\\', "/")),
            pages: 0,
            error: None,
        })
        .collect();
    persist_atomic(
        &destination_root.join("import-report.json"),
        &serde_json::to_vec_pretty(&outcomes)?,
        false,
    )?;
    for (index, (source, title, relative, original)) in jobs.into_iter().enumerate() {
        println!("Importing {title}");
        let result = (|| -> Result<usize> {
            let job = tempfile::Builder::new()
                .prefix("notebook-")
                .tempdir_in(scratch.path())?;
            let stable_source = job.path().join("source.goodnotes");
            persist_atomic(&stable_source, &read_limited(&source, 128 * MIB)?, false)?;
            let decoded = job.path().join("decoded");
            decoder(&runtime, "decode", &stable_source, &decoded)?;
            let staged = job.path().join("notebook");
            let report = convert(&decoded, &stable_source, &staged, &title, allow_derived)?;
            let target = destination_root.join(&relative);
            let folder = target.parent().ok_or("target has no parent")?;
            fs::create_dir_all(folder)?;
            paths::contained(&destination_root, folder, "destination folder")?;
            publish(&staged, &target)?;
            Ok(report.pages.len())
        })();
        let outcome = match result {
            Ok(pages) => {
                println!("Imported {pages} pages into {}", relative.display());
                Outcome {
                    status: "completed",
                    source: original,
                    target: Some(relative.to_string_lossy().replace('\\', "/")),
                    pages,
                    error: None,
                }
            }
            Err(error) => {
                eprintln!("Failed {title}: {error}");
                Outcome {
                    status: "failed",
                    source: original,
                    target: None,
                    pages: 0,
                    error: Some(error.to_string()),
                }
            }
        };
        outcomes[index] = outcome;
        persist_atomic(
            &destination_root.join("import-report.json"),
            &serde_json::to_vec_pretty(&outcomes)?,
            true,
        )?;
    }
    if outcomes.iter().any(|o| o.error.is_some()) {
        return Err(format!(
            "some notebooks failed; completed notebooks are preserved. See {}",
            destination_root.join("import-report.json").display()
        )
        .into());
    }
    println!(
        "Done. Open {} in the existing Goodtype Library.",
        destination_root.display()
    );
    Ok(())
}

fn decoder(runtime: &runtime::Runtime, mode: &str, source: &Path, output: &Path) -> Result<()> {
    let mut command = Command::new(&runtime.python);
    command.arg("-I").arg(&runtime.adapter).arg(mode);
    if mode != "check" {
        command.arg(source).arg(output);
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x0800_0000); // CREATE_NO_WINDOW: safe to launch from the desktop app.
    }
    let mut child = command
        .stdin(Stdio::null())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit())
        .spawn()?;
    let _limit = match process_limit::ProcessLimit::attach(&child) {
        Ok(limit) => limit,
        Err(error) => {
            child.kill()?;
            child.wait()?;
            return Err(format!("could not apply decoder memory limit: {error}").into());
        }
    };
    let started = Instant::now();
    loop {
        if let Some(status) = child.try_wait()? {
            return if status.success() {
                Ok(())
            } else {
                Err(format!("decoder failed ({status}); notebook was not published").into())
            };
        }
        if started.elapsed() > Duration::from_secs(300) {
            child.kill()?;
            child.wait()?;
            return Err(
                "decoder exceeded the five-minute limit; notebook was not published".into(),
            );
        }
        thread::sleep(Duration::from_millis(100));
    }
}
