use std::{env, path::Path, process::Command};

fn main() {
    if let Err(error) = verify() {
        eprintln!("{error}");
        std::process::exit(1);
    }
}

fn verify() -> Result<(), String> {
    if env::args().skip(1).collect::<Vec<_>>() != ["verify"] {
        return Err("usage: cargo xtask verify".into());
    }
    let root = Path::new(env!("CARGO_MANIFEST_DIR"));
    let python = if cfg!(windows) {
        ".venv/Scripts/python.exe"
    } else {
        ".venv/bin/python"
    };
    for (program, arguments) in [
        ("cargo", vec!["fmt", "--", "--check"]),
        (
            "cargo",
            vec![
                "clippy",
                "--locked",
                "--all-targets",
                "--",
                "-D",
                "warnings",
            ],
        ),
        ("cargo", vec!["test", "--locked"]),
        (python, vec!["-I", "test_decode.py"]),
        (python, vec!["-I", "scripts/test_build.py"]),
    ] {
        let status = Command::new(program)
            .args(arguments)
            .current_dir(root)
            .status()
            .map_err(|error| format!("could not run {program}: {error}"))?;
        if !status.success() {
            return Err(format!("verification failed: {program} ({status})"));
        }
    }
    println!("Verification passed.");
    Ok(())
}
