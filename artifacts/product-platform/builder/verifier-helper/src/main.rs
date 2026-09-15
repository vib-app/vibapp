use std::env;
use std::fs;
use std::process::ExitCode;

use vibapp_protocol::parse_manifest;

const MAX_MANIFEST_BYTES: u64 = 1_048_576;

fn main() -> ExitCode {
    let Some(path) = env::args_os().nth(1) else {
        eprintln!("manifest path is required");
        return ExitCode::from(64);
    };

    let metadata = match fs::metadata(&path) {
        Ok(value) => value,
        Err(error) => {
            eprintln!("manifest metadata failed: {error}");
            return ExitCode::from(65);
        }
    };
    if !metadata.is_file() || metadata.len() > MAX_MANIFEST_BYTES {
        eprintln!("manifest is not a bounded regular file");
        return ExitCode::from(65);
    }

    let text = match fs::read_to_string(&path) {
        Ok(value) => value,
        Err(error) => {
            eprintln!("manifest read failed: {error}");
            return ExitCode::from(65);
        }
    };
    match parse_manifest(&text) {
        Ok(manifest) => {
            println!(
                "MANIFEST_PROTOCOL_PASS app={} world={}",
                manifest.app.id,
                manifest.runtime.world.as_str()
            );
            ExitCode::SUCCESS
        }
        Err(error) => {
            eprintln!(
                "manifest protocol rejection: code={:?} path={} message={}",
                error.code, error.path, error.detail
            );
            ExitCode::from(65)
        }
    }
}
