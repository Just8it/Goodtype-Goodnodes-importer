use std::path::{Path, PathBuf};

use goodtype_goodnotes_import::Result;

pub struct Runtime {
    pub python: PathBuf,
    pub adapter: PathBuf,
}

pub fn locate() -> Result<Runtime> {
    let executable = std::env::current_exe()?;
    let installed = executable
        .parent()
        .ok_or("importer executable has no parent")?;
    resolve(
        installed,
        if cfg!(debug_assertions) {
            Some(Path::new(env!("CARGO_MANIFEST_DIR")))
        } else {
            None
        },
    )
}

fn resolve(installed: &Path, development: Option<&Path>) -> Result<Runtime> {
    let python = installed.join(if cfg!(windows) {
        "runtime/python.exe"
    } else {
        "runtime/bin/python3"
    });
    let adapter = installed.join("decode.py");
    if python.is_file() && adapter.is_file() {
        return Ok(Runtime { python, adapter });
    }
    if let Some(root) = development {
        let python = root.join(if cfg!(windows) {
            ".venv/Scripts/python.exe"
        } else {
            ".venv/bin/python"
        });
        let adapter = root.join("decode.py");
        if python.is_file() && adapter.is_file() {
            return Ok(Runtime { python, adapter });
        }
    }
    Err(
        "importer runtime is missing; extract the complete release ZIP and keep its files together"
            .into(),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn installed_bundle_is_relocatable_and_does_not_need_the_checkout() {
        let temp = tempfile::tempdir().unwrap();
        let bundle = temp.path().join("folder with spaces");
        let python = bundle.join(if cfg!(windows) {
            "runtime/python.exe"
        } else {
            "runtime/bin/python3"
        });
        std::fs::create_dir_all(python.parent().unwrap()).unwrap();
        std::fs::write(&python, b"runtime fixture").unwrap();
        std::fs::write(bundle.join("decode.py"), b"adapter fixture").unwrap();
        let runtime = resolve(&bundle, None).unwrap();
        assert_eq!(runtime.python, python);
        assert_eq!(runtime.adapter, bundle.join("decode.py"));
        assert!(resolve(temp.path(), None).is_err());
    }
}
