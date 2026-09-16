//! Explicit local Launcher entry points; never installation or permission grants.
use std::ffi::OsString;
use std::path::{Component, Path, PathBuf};

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct StartupOptions {
    pub app_id: Option<String>,
    pub store_app_id: Option<String>,
    pub data_dir: Option<PathBuf>,
    pub help: bool,
}

pub const USAGE: &str = "VibApp [--open-app APP_ID | vibapp://APP_ID] [--data-dir ABSOLUTE_PRIVATE_DIRECTORY]\n\
    --open-app opens an already enabled application through the normal Launcher.\n\
    --data-dir selects an existing owner-private workspace; no user data is copied.\n\
    App links open installed apps or request Store download and explicit install confirmation.\n\
    Links never grant permissions by themselves.";

pub fn app_id_from_url(url: &str) -> Result<String, String> {
    let id = url.strip_prefix("vibapp://").ok_or("Unsupported app link")?;
    let id = id.strip_suffix('/').unwrap_or(id);
    if id.is_empty() || id.len() > 128 || !id.as_bytes()[0].is_ascii_lowercase()
        || !id.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b".-".contains(&b))
        || id.split(['.', '-']).any(str::is_empty)
    {
        return Err("App link must contain only an application ID".into());
    }
    Ok(id.to_string())
}

pub fn require_installed_application(item: &serde_json::Value) -> Result<(), String> {
    if item["installation_state"] != "installed" || item["enabled"] != true
        || item["launch_eligible"] != true
    {
        return Err("--open-app requires an installed, enabled application with a UI entrypoint".into());
    }
    Ok(())
}

impl StartupOptions {
    pub fn parse(arguments: impl IntoIterator<Item = OsString>) -> Result<Self, String> {
        let mut options = Self::default();
        let mut arguments = arguments.into_iter();
        let mut count = 0;
        while let Some(argument) = arguments.next() {
            count += 1;
            if count > 3 {
                return Err("Too many Launcher options".into());
            }
            match argument.to_str() {
                Some("--help" | "-h") if !options.help => options.help = true,
                Some("--open-app") if options.app_id.is_none() => {
                    let app_id = arguments.next().and_then(|value| value.into_string().ok())
                        .ok_or("--open-app requires an application ID")?;
                    if app_id.is_empty() || app_id.len() > 200
                        || !app_id.bytes().all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || b".-_".contains(&byte))
                        || !app_id.as_bytes()[0].is_ascii_lowercase()
                    {
                        return Err("Invalid Launcher application ID".into());
                    }
                    options.app_id = Some(app_id);
                }
                Some("--data-dir") if options.data_dir.is_none() => {
                    let path = PathBuf::from(arguments.next().ok_or("--data-dir requires an absolute directory")?);
                    validate_private_directory(&path)?;
                    options.data_dir = Some(path);
                }
                Some(url) if url.starts_with("vibapp://") && options.store_app_id.is_none() => {
                    options.store_app_id = Some(app_id_from_url(url)?);
                }
                _ => return Err("Unknown or repeated Launcher option; use --help".into()),
            }
        }
        if options.app_id.is_some() && options.store_app_id.is_some() {
            return Err("Choose either --open-app or an app link".into());
        }
        Ok(options)
    }
}

fn validate_private_directory(path: &Path) -> Result<(), String> {
    if !path.is_absolute() || path.parent().is_none()
        || path.components().any(|part| matches!(part, Component::CurDir | Component::ParentDir))
    {
        return Err("Workspace must be an absolute existing private directory".into());
    }
    let metadata = std::fs::symlink_metadata(path).map_err(|_| "Workspace directory does not exist")?;
    if !metadata.is_dir() || metadata.file_type().is_symlink() {
        return Err("Workspace directory must not contain symbolic links".into());
    }
    #[cfg(not(windows))]
    if std::fs::canonicalize(path).map_err(|_| "Cannot resolve workspace directory")? != path {
        return Err("Workspace directory must not contain symbolic links".into());
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        use std::path::Prefix;
        // canonicalize adds \\?\ to ordinary Windows paths. Inspect ancestry
        // without following reparse points instead of comparing unlike forms.
        if !matches!(path.components().next(), Some(Component::Prefix(prefix)) if matches!(prefix.kind(), Prefix::Disk(_) | Prefix::VerbatimDisk(_))) {
            return Err("Workspace must use an absolute local Windows drive".into());
        }
        for ancestor in path.ancestors() {
            let info = std::fs::symlink_metadata(ancestor).map_err(|_| "Cannot inspect workspace ancestry")?;
            if !info.is_dir() || info.file_type().is_symlink() || info.file_attributes() & 0x400 != 0 {
                return Err("Workspace ancestry must not contain Windows reparse points".into());
            }
        }
        // Selection is read-only: never repair a caller-selected workspace ACL.
        crate::native_platform::verify_private_directory(path)?;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        // SAFETY: geteuid has no pointer arguments and only returns the caller identity.
        if metadata.uid() != unsafe { libc::geteuid() } || metadata.mode() & 0o077 != 0 {
            return Err("Workspace must be owned by this user with mode 0700".into());
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(arguments: &[&str]) -> Result<StartupOptions, String> {
        StartupOptions::parse(arguments.iter().map(OsString::from))
    }

    #[test]
    fn default_startup_preserves_normal_user_workspace() {
        assert_eq!(parse(&[]).unwrap(), StartupOptions::default());
        assert!(parse(&["--help"]).unwrap().help);
    }

    #[test]
    fn app_links_are_identity_only_and_cannot_add_authority() {
        for url in ["vibapp://ai.vibapp.clock", "vibapp://ai.vibapp.clock/"] {
            assert_eq!(app_id_from_url(url).unwrap(), "ai.vibapp.clock");
            assert_eq!(parse(&[url]).unwrap().store_app_id.as_deref(), Some("ai.vibapp.clock"));
        }
        for url in ["https://app", "vibapp://", "vibapp://app/path", "vibapp://app?install=true",
                    "vibapp://app#x", "vibapp://user@app", "vibapp://app:80", "vibapp://%61pp",
                    "vibapp://app..id", "vibapp://app-", "vibapp://APP", "vibapp://app//"] {
            assert!(app_id_from_url(url).is_err(), "{url}");
        }
        assert!(parse(&["--open-app", "app", "vibapp://app"]).is_err());
    }

    #[test]
    fn direct_startup_never_promotes_a_preview_candidate_or_disabled_app() {
        use serde_json::json;
        let installed = json!({"installation_state":"installed", "enabled":true, "launch_eligible":true});
        assert!(require_installed_application(&installed).is_ok());
        for state in ["preview", "candidate", "installed-disabled", ""] {
            let mut item = installed.clone();
            item["installation_state"] = json!(state);
            assert!(require_installed_application(&item).is_err());
        }
        for field in ["enabled", "launch_eligible"] {
            let mut item = installed.clone();
            item[field] = json!(false);
            assert!(require_installed_application(&item).is_err());
        }
        assert!(require_installed_application(&json!({})).is_err());
    }

    #[test]
    fn open_app_is_only_an_identity_not_a_script_or_path() {
        assert_eq!(parse(&["--open-app", "ai.vibapp.everyday.clock"]).unwrap().app_id.as_deref(), Some("ai.vibapp.everyday.clock"));
        for value in ["", "../app", "app?script=1", "file:///tmp/app", "--help", " app", "app\nnext"] {
            assert!(parse(&["--open-app", value]).is_err());
        }
    }

    #[test]
    fn unknown_duplicate_or_incomplete_options_fail_before_gui() {
        for arguments in [vec!["--open-app"], vec!["--data-dir"], vec!["--install", "app"],
                          vec!["--open-app", "app", "--open-app", "other"], vec!["--help", "--help"]] {
            assert!(parse(&arguments).is_err());
        }
    }

    #[test]
    fn rejects_relative_parent_or_missing_workspace() {
        for path in [".", "../workspace", "/", "/definitely-absent-vibapp-startup-workspace"] {
            assert!(parse(&["--data-dir", path]).is_err());
        }
    }

    #[cfg(unix)]
    #[test]
    fn accepts_private_owner_directory_without_changing_its_contents() {
        use std::os::unix::fs::{DirBuilderExt, PermissionsExt, symlink};
        let root = std::env::temp_dir().canonicalize().unwrap().join(format!("vibapp-startup-{}", uuid::Uuid::new_v4()));
        std::fs::DirBuilder::new().mode(0o700).create(&root).unwrap();
        let before = std::fs::metadata(&root).unwrap().permissions().mode();
        let parsed = StartupOptions::parse([OsString::from("--data-dir"), root.clone().into_os_string()]).unwrap();
        assert_eq!(parsed.data_dir, Some(root.clone()));
        assert_eq!(std::fs::read_dir(&root).unwrap().count(), 0);
        assert_eq!(std::fs::metadata(&root).unwrap().permissions().mode(), before);
        let linked = root.join("link");
        symlink(&root, &linked).unwrap();
        assert!(validate_private_directory(&linked).is_err());
        std::fs::remove_file(linked).unwrap();
        std::fs::set_permissions(&root, std::fs::Permissions::from_mode(0o755)).unwrap();
        assert!(validate_private_directory(&root).is_err());
        std::fs::remove_dir(&root).unwrap();
    }
}
