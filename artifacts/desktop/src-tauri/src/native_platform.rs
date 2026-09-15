use std::ffi::OsString;
use std::fs;
use std::path::{Component, Path, PathBuf};
use std::process::{Command, Stdio};

#[cfg(unix)]
use std::os::unix::fs::PermissionsExt;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum HostOs {
    Macos,
    Linux,
    Windows,
    Unsupported,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ResourceKind {
    File,
    Directory,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct PackagedLayout {
    package_root: PathBuf,
    resources_relative: PathBuf,
    service_runtime_relative: PathBuf,
}

#[derive(Clone, Debug, Eq, PartialEq)]
enum DaemonTransportPolicy {
    UnixOwnerSocket { path: PathBuf },
    WindowsOwnerNamedPipeRequired { pipe_name: String },
    Unsupported,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum SecretProtectionPolicy {
    UnixOwnerMode,
    WindowsOwnerAclRequired,
    Unsupported,
}

fn current_os() -> HostOs {
    #[cfg(target_os = "macos")]
    {
        HostOs::Macos
    }
    #[cfg(target_os = "linux")]
    {
        HostOs::Linux
    }
    #[cfg(target_os = "windows")]
    {
        HostOs::Windows
    }
    #[cfg(not(any(target_os = "macos", target_os = "linux", target_os = "windows")))]
    {
        HostOs::Unsupported
    }
}

fn filename(path: &Path) -> Option<&str> {
    path.file_name().and_then(|value| value.to_str())
}

fn packaged_layout_for(os: HostOs, executable: &Path) -> Option<PackagedLayout> {
    let parent = executable.parent()?;
    match os {
        HostOs::Macos
            if filename(parent) == Some("MacOS")
                && parent.parent().and_then(filename) == Some("Contents")
                && parent
                    .parent()
                    .and_then(Path::parent)
                    .and_then(filename)
                    .is_some_and(|name| name.ends_with(".app")) =>
        {
            Some(PackagedLayout {
                package_root: parent.parent()?.parent()?.to_path_buf(),
                resources_relative: PathBuf::from("Contents/Resources"),
                service_runtime_relative: PathBuf::from(
                    "Contents/Resources/runtime-daemon/service-runtime/vibapp-service-runtime",
                ),
            })
        }
        HostOs::Linux
            if filename(parent) == Some("bin")
                && parent.parent().and_then(filename) == Some("vibapp") =>
        {
            Some(PackagedLayout {
                package_root: parent.parent()?.to_path_buf(),
                resources_relative: PathBuf::from("resources"),
                service_runtime_relative: PathBuf::from("libexec/vibapp-service-runtime"),
            })
        }
        HostOs::Windows
            if filename(parent) == Some("VibApp")
                && filename(executable) == Some("vibapp-launcher.exe") =>
        {
            Some(PackagedLayout {
                package_root: parent.to_path_buf(),
                resources_relative: PathBuf::from("resources"),
                service_runtime_relative: PathBuf::from("libexec/vibapp-service-runtime.exe"),
            })
        }
        _ => None,
    }
}

fn packaged_resource_root(os: HostOs, executable: &Path) -> Option<Result<PathBuf, String>> {
    packaged_layout_for(os, executable)
        .map(|layout| Ok(layout.package_root.join(layout.resources_relative)))
}

fn safe_resource_relative(relative: &Path) -> bool {
    !relative.as_os_str().is_empty()
        && !relative.is_absolute()
        && relative
            .components()
            .all(|component| matches!(component, Component::Normal(_)))
}

fn ordinary_path(path: &Path, kind: ResourceKind) -> bool {
    let Ok(metadata) = fs::symlink_metadata(path) else {
        return false;
    };
    if metadata.file_type().is_symlink() {
        return false;
    }
    match kind {
        ResourceKind::File => metadata.file_type().is_file(),
        ResourceKind::Directory => metadata.file_type().is_dir(),
    }
}

fn contained_ordinary_path(anchor: &Path, relative: &Path, kind: ResourceKind) -> Option<PathBuf> {
    if !safe_resource_relative(relative) {
        return None;
    }
    if !ordinary_path(anchor, ResourceKind::Directory) {
        return None;
    }

    let mut candidate = anchor.to_path_buf();
    let mut components = relative.components().peekable();
    while let Some(component) = components.next() {
        let Component::Normal(component) = component else {
            return None;
        };
        candidate.push(component);
        let expected_kind = if components.peek().is_some() {
            ResourceKind::Directory
        } else {
            kind
        };
        if !ordinary_path(&candidate, expected_kind) {
            return None;
        }
    }

    // Canonicalization is deliberately last: every trusted component, including
    // the anchor and leaf, has already been inspected without following links.
    let canonical_root = fs::canonicalize(anchor).ok()?;
    let canonical_candidate = fs::canonicalize(&candidate).ok()?;
    canonical_candidate
        .starts_with(&canonical_root)
        .then_some(canonical_candidate)
}

fn resolve_resource_for(
    os: HostOs,
    executable: &Path,
    packaged_relative: &Path,
    development: &Path,
    kind: ResourceKind,
) -> Result<PathBuf, String> {
    if !safe_resource_relative(packaged_relative) {
        return Err("打包资源路径不是安全的相对路径。".to_string());
    }
    if let Some(layout) = packaged_layout_for(os, executable) {
        let resource_relative = layout.resources_relative.join(packaged_relative);
        return contained_ordinary_path(&layout.package_root, &resource_relative, kind).ok_or_else(
            || {
                format!(
                    "打包资源缺失、越界或文件类型无效：{}；已拒绝回退到开发目录。",
                    layout.package_root.join(resource_relative).display()
                )
            },
        );
    }
    if ordinary_path(development, kind) {
        fs::canonicalize(development)
            .map_err(|error| format!("无法解析本地开发资源 {}：{error}", development.display()))
    } else {
        Err(format!(
            "本地开发资源不存在或文件类型无效：{}",
            development.display()
        ))
    }
}

pub fn resource_file(packaged: &str, development: &Path) -> Result<PathBuf, String> {
    let executable =
        std::env::current_exe().map_err(|error| format!("无法定位 VibApp Launcher：{error}"))?;
    resolve_resource_for(
        current_os(),
        &executable,
        Path::new(packaged),
        development,
        ResourceKind::File,
    )
}

pub fn resource_directory(packaged: &str, development: &Path) -> Result<PathBuf, String> {
    let executable =
        std::env::current_exe().map_err(|error| format!("无法定位 VibApp Launcher：{error}"))?;
    resolve_resource_for(
        current_os(),
        &executable,
        Path::new(packaged),
        development,
        ResourceKind::Directory,
    )
}

fn packaged_service_runtime_for(os: HostOs, executable: &Path) -> Option<PathBuf> {
    let layout = packaged_layout_for(os, executable)?;
    Some(layout.package_root.join(layout.service_runtime_relative))
}

fn resolve_service_runtime_for(
    os: HostOs,
    executable: &Path,
    development: &Path,
) -> Result<PathBuf, String> {
    if let Some(layout) = packaged_layout_for(os, executable) {
        let candidate = layout.package_root.join(&layout.service_runtime_relative);
        if let Some(resolved) = contained_ordinary_path(
            &layout.package_root,
            &layout.service_runtime_relative,
            ResourceKind::File,
        ) {
            return Ok(resolved);
        }
        return Err(format!(
            "打包 service runtime 缺失或文件类型无效：{}；已拒绝回退到开发目录。",
            candidate.display()
        ));
    }
    if ordinary_path(development, ResourceKind::File) {
        fs::canonicalize(development).map_err(|error| {
            format!(
                "无法解析开发 service runtime {}：{error}",
                development.display()
            )
        })
    } else {
        Err(format!(
            "开发 service runtime 不存在或文件类型无效：{}",
            development.display()
        ))
    }
}

pub fn service_runtime_binary(development: &Path) -> Result<PathBuf, String> {
    let executable =
        std::env::current_exe().map_err(|error| format!("无法定位 VibApp Launcher：{error}"))?;
    resolve_service_runtime_for(current_os(), &executable, development)
}

fn python_candidates(os: HostOs) -> &'static [&'static str] {
    match os {
        HostOs::Macos => &[
            "/opt/homebrew/bin/python3",
            "/usr/local/bin/python3",
            "/usr/bin/python3",
        ],
        HostOs::Linux => &["/usr/bin/python3", "/usr/local/bin/python3"],
        // Windows discovery is intentionally not delegated to PATH, py.exe, the
        // registry, or a user-writable install directory. A packaged/ACL-verified
        // interpreter policy must land before Python-backed product features run.
        HostOs::Windows | HostOs::Unsupported => &[],
    }
}

fn trusted_tool_install_anchor(os: HostOs, source: &Path) -> Option<&'static Path> {
    let locations: &[(&str, &str)] = match os {
        HostOs::Macos => &[
            ("/opt/homebrew/bin", "/opt/homebrew"),
            ("/usr/local/bin", "/usr/local"),
            ("/usr/bin", "/usr"),
        ],
        HostOs::Linux => &[("/usr/local/bin", "/usr/local"), ("/usr/bin", "/usr")],
        HostOs::Windows | HostOs::Unsupported => &[],
    };
    locations
        .iter()
        .find(|(directory, _)| source.parent() == Some(Path::new(directory)))
        .map(|(_, anchor)| Path::new(anchor))
}

fn resolve_trusted_tool_candidate(source: &Path, install_anchor: &Path) -> Option<PathBuf> {
    if !source.is_absolute() || !install_anchor.is_absolute() {
        return None;
    }
    let source_relative = source.strip_prefix(install_anchor).ok()?;
    if !safe_resource_relative(source_relative) {
        return None;
    }

    // Inspect the configured candidate before canonicalization. An ordinary file
    // must have an entirely ordinary ancestry inside the installation domain.
    // A leaf symlink is allowed only as an explicit package-manager alias whose
    // final target remains in that same domain; this preserves standard Homebrew
    // aliases without permitting /opt/homebrew/bin or /usr/local/bin links to
    // escape to an unrelated tree.
    let source_metadata = fs::symlink_metadata(source).ok()?;
    if source_metadata.file_type().is_file() {
        return contained_ordinary_path(install_anchor, source_relative, ResourceKind::File);
    }
    if !source_metadata.file_type().is_symlink() {
        return None;
    }

    let source_parent = source.parent()?;
    let parent_relative = source_parent.strip_prefix(install_anchor).ok()?;
    if !safe_resource_relative(parent_relative)
        || contained_ordinary_path(install_anchor, parent_relative, ResourceKind::Directory)
            .is_none()
    {
        return None;
    }

    let canonical_anchor = fs::canonicalize(install_anchor).ok()?;
    let canonical_target = fs::canonicalize(source).ok()?;
    let target_relative = canonical_target.strip_prefix(&canonical_anchor).ok()?;
    contained_ordinary_path(&canonical_anchor, target_relative, ResourceKind::File)
}

fn trusted_tool_candidate_for(os: HostOs, source: &Path) -> Option<PathBuf> {
    let anchor = trusted_tool_install_anchor(os, source)?;
    resolve_trusted_tool_candidate(source, anchor)
}

pub fn safe_path() -> Result<OsString, String> {
    match current_os() {
        HostOs::Macos => Ok(OsString::from(
            "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        )),
        HostOs::Linux => Ok(OsString::from("/usr/local/bin:/usr/bin:/bin")),
        HostOs::Windows => Err(
            "Windows 受信任 PATH 与 helper 策略尚未实现；已拒绝启动 Python helper。".to_string(),
        ),
        HostOs::Unsupported => Err("当前平台没有受支持的 helper 搜索策略。".to_string()),
    }
}

pub fn python_executable(require_tomllib: bool) -> Result<PathBuf, String> {
    // Release distributions carry an isolated interpreter. Never let a broken
    // or redirected bundled interpreter silently select a developer tool.
    let executable = std::env::current_exe().map_err(|error| error.to_string())?;
    if let Some(layout) = packaged_layout_for(current_os(), &executable) {
        let relative = layout.resources_relative.join("python");
        if fs::symlink_metadata(layout.package_root.join(&relative)).is_ok() {
            let python = contained_ordinary_path(
                &layout.package_root,
                &relative.join("bin/python3.13"),
                ResourceKind::File,
            ).ok_or_else(|| "打包的 Python 运行时缺失或路径无效。".to_string())?;
            return if python_supported(&python, require_tomllib)? {
                Ok(python)
            } else {
                Err("打包的 Python 运行时无法启动，请重新安装 VibApp。".to_string())
            };
        }
    }
    for candidate in python_candidates(current_os()) {
        let source = Path::new(candidate);
        let Some(path) = trusted_tool_candidate_for(current_os(), source) else {
            continue;
        };
        if python_supported(&path, require_tomllib)? {
            return Ok(path);
        }
    }
    match current_os() {
        HostOs::Windows => Err(
            "Windows Python 3.11+ 仅允许未来的打包且 owner-ACL 验证路径；当前已停止。".to_string(),
        ),
        _ => Err("未找到受信任的 Python 3.11+ 运行时。".to_string()),
    }
}

fn python_supported(path: &Path, require_tomllib: bool) -> Result<bool, String> {
        let probe = if require_tomllib {
            "import sys,tomllib;raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
        } else {
            "import sys;raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
        };
        let supported = Command::new(&path)
            .args(["-I", "-B", "-c", probe])
            .env_clear()
            .env("PATH", safe_path()?)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .is_ok_and(|status| status.success());
        Ok(supported)
}

fn trusted_helper_candidates(os: HostOs, binary: &str) -> Vec<PathBuf> {
    let directories: &[&str] = match os {
        HostOs::Macos => &["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"],
        HostOs::Linux => &["/usr/local/bin", "/usr/bin"],
        HostOs::Windows | HostOs::Unsupported => &[],
    };
    directories
        .iter()
        .map(|directory| Path::new(directory).join(binary))
        .collect()
}

pub fn trusted_helper_path(binary: &str) -> Option<PathBuf> {
    if binary.is_empty()
        || !binary
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
    {
        return None;
    }
    trusted_helper_candidates(current_os(), binary)
        .iter()
        .find_map(|candidate| trusted_tool_candidate_for(current_os(), candidate))
}

pub fn trusted_helper_available(binary: &str) -> bool {
    trusted_helper_path(binary).is_some()
}

fn daemon_transport_policy_for(os: HostOs, data_dir: &Path) -> DaemonTransportPolicy {
    match os {
        HostOs::Macos | HostOs::Linux => DaemonTransportPolicy::UnixOwnerSocket {
            path: data_dir.join("runtime-daemon/run/vibappd.sock"),
        },
        HostOs::Windows => DaemonTransportPolicy::WindowsOwnerNamedPipeRequired {
            pipe_name: r"\\.\pipe\vibappd-owner-v1-{owner-sid-sha256}".to_string(),
        },
        HostOs::Unsupported => DaemonTransportPolicy::Unsupported,
    }
}

pub fn unix_daemon_socket(data_dir: &Path) -> Result<PathBuf, String> {
    match daemon_transport_policy_for(current_os(), data_dir) {
        DaemonTransportPolicy::UnixOwnerSocket { path } => Ok(path),
        DaemonTransportPolicy::WindowsOwnerNamedPipeRequired { pipe_name } => Err(format!(
            "Windows daemon 需要 owner-only named pipe {pipe_name}；当前没有实现 ACL/客户端身份校验，已停止。"
        )),
        DaemonTransportPolicy::Unsupported => {
            Err("当前平台没有 owner-authenticated daemon transport。".to_string())
        }
    }
}

fn secret_protection_policy_for(os: HostOs) -> SecretProtectionPolicy {
    match os {
        HostOs::Macos | HostOs::Linux => SecretProtectionPolicy::UnixOwnerMode,
        HostOs::Windows => SecretProtectionPolicy::WindowsOwnerAclRequired,
        HostOs::Unsupported => SecretProtectionPolicy::Unsupported,
    }
}

pub fn protect_private_directory(path: &Path) -> Result<(), String> {
    match secret_protection_policy_for(current_os()) {
        SecretProtectionPolicy::UnixOwnerMode => {
            #[cfg(unix)]
            {
                fs::set_permissions(path, fs::Permissions::from_mode(0o700))
                    .map_err(|error| format!("无法限制私有目录权限：{error}"))
            }
            #[cfg(not(unix))]
            {
                let _ = path;
                Err("Unix owner-mode 策略无法在当前构建目标执行。".to_string())
            }
        }
        SecretProtectionPolicy::WindowsOwnerAclRequired => Err(
            "Windows owner-only secret ACL 尚未实现；已拒绝创建或使用 BYOM 密钥存储。".to_string(),
        ),
        SecretProtectionPolicy::Unsupported => Err("当前平台没有私有目录保护策略。".to_string()),
    }
}

pub fn protect_private_file(path: &Path) -> Result<(), String> {
    match secret_protection_policy_for(current_os()) {
        SecretProtectionPolicy::UnixOwnerMode => {
            #[cfg(unix)]
            {
                fs::set_permissions(path, fs::Permissions::from_mode(0o600))
                    .map_err(|error| format!("无法限制私有文件权限：{error}"))
            }
            #[cfg(not(unix))]
            {
                let _ = path;
                Err("Unix owner-mode 策略无法在当前构建目标执行。".to_string())
            }
        }
        SecretProtectionPolicy::WindowsOwnerAclRequired => {
            Err("Windows owner-only secret ACL 尚未实现；已拒绝持久化 BYOM 密钥。".to_string())
        }
        SecretProtectionPolicy::Unsupported => Err("当前平台没有私有文件保护策略。".to_string()),
    }
}

pub fn verify_private_file(path: &Path) -> Result<(), String> {
    match secret_protection_policy_for(current_os()) {
        SecretProtectionPolicy::UnixOwnerMode => {
            #[cfg(unix)]
            {
                let metadata = fs::symlink_metadata(path)
                    .map_err(|error| format!("无法检查私有文件保护：{error}"))?;
                if !metadata.file_type().is_file()
                    || metadata.file_type().is_symlink()
                    || metadata.permissions().mode() & 0o077 != 0
                {
                    return Err("私有文件不是 owner-only 普通文件；已拒绝读取。".to_string());
                }
                Ok(())
            }
            #[cfg(not(unix))]
            {
                let _ = path;
                Err("Unix owner-mode 策略无法在当前构建目标执行。".to_string())
            }
        }
        SecretProtectionPolicy::WindowsOwnerAclRequired => {
            Err("Windows owner-only secret ACL 尚未实现；已拒绝读取 BYOM 密钥。".to_string())
        }
        SecretProtectionPolicy::Unsupported => Err("当前平台没有私有文件保护策略。".to_string()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn temp_dir(label: &str) -> PathBuf {
        let path = std::env::temp_dir().join(format!(
            "vibapp-native-platform-{label}-{}-{:x}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_nanos()
        ));
        fs::create_dir_all(&path).expect("create temporary root");
        path
    }

    #[test]
    fn accepted_package_layouts_select_platform_resource_roots() {
        let root = Path::new("/tmp/native-layouts");
        assert_eq!(
            packaged_resource_root(
                HostOs::Macos,
                &root.join("VibApp.app/Contents/MacOS/vibapp-launcher")
            ),
            Some(Ok(root.join("VibApp.app/Contents/Resources")))
        );
        assert_eq!(
            packaged_resource_root(HostOs::Linux, &root.join("vibapp/bin/vibapp-launcher")),
            Some(Ok(root.join("vibapp/resources")))
        );
        assert_eq!(
            packaged_resource_root(HostOs::Windows, &root.join("VibApp/vibapp-launcher.exe")),
            Some(Ok(root.join("VibApp/resources")))
        );
        assert_eq!(
            packaged_service_runtime_for(
                HostOs::Macos,
                &root.join("VibApp.app/Contents/MacOS/vibapp-launcher")
            ),
            Some(
                root.join(
                    "VibApp.app/Contents/Resources/runtime-daemon/service-runtime/vibapp-service-runtime"
                )
            )
        );
        assert_eq!(
            packaged_service_runtime_for(HostOs::Linux, &root.join("vibapp/bin/vibapp-launcher")),
            Some(root.join("vibapp/libexec/vibapp-service-runtime"))
        );
        assert_eq!(
            packaged_service_runtime_for(HostOs::Windows, &root.join("VibApp/vibapp-launcher.exe")),
            Some(root.join("VibApp/libexec/vibapp-service-runtime.exe"))
        );
    }

    #[test]
    fn cargo_target_layout_is_development_not_packaged() {
        let root = Path::new("/tmp/native-layouts");
        assert_eq!(
            packaged_resource_root(HostOs::Macos, &root.join("target/debug/vibapp-launcher")),
            None
        );
        assert_eq!(
            packaged_resource_root(HostOs::Linux, &root.join("target/release/vibapp-launcher")),
            None
        );
        assert_eq!(
            packaged_resource_root(
                HostOs::Windows,
                &root.join("target/release/vibapp-launcher.exe")
            ),
            None
        );
    }

    #[test]
    fn missing_packaged_resource_never_falls_back_to_development_tree() {
        let root = temp_dir("no-fallback");
        let executable = root.join("vibapp/bin/vibapp-launcher");
        fs::create_dir_all(executable.parent().unwrap()).unwrap();
        fs::write(&executable, b"launcher").unwrap();
        let development = root.join("development/helper.py");
        fs::create_dir_all(development.parent().unwrap()).unwrap();
        fs::write(&development, b"print('should not run')\n").unwrap();
        let error = resolve_resource_for(
            HostOs::Linux,
            &executable,
            Path::new("helper/helper.py"),
            &development,
            ResourceKind::File,
        )
        .unwrap_err();
        assert!(error.contains("拒绝回退"));
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn development_layout_uses_only_an_ordinary_requested_path() {
        let root = temp_dir("development");
        let development = root.join("helper.py");
        fs::write(&development, b"print('ok')\n").unwrap();
        let resolved = resolve_resource_for(
            HostOs::Linux,
            &root.join("target/debug/vibapp-launcher"),
            Path::new("helper/helper.py"),
            &development,
            ResourceKind::File,
        )
        .unwrap();
        assert_eq!(resolved, fs::canonicalize(&development).unwrap());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn packaged_relative_paths_cannot_escape_the_resource_root() {
        let root = temp_dir("escape");
        let executable = root.join("VibApp.app/Contents/MacOS/vibapp-launcher");
        fs::create_dir_all(executable.parent().unwrap()).unwrap();
        fs::write(&executable, b"launcher").unwrap();
        let development = root.join("dev.py");
        fs::write(&development, b"print('dev')\n").unwrap();
        assert!(
            resolve_resource_for(
                HostOs::Macos,
                &executable,
                Path::new("../dev.py"),
                &development,
                ResourceKind::File,
            )
            .is_err()
        );
        fs::remove_dir_all(root).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn packaged_resource_symlink_is_rejected_without_development_fallback() {
        use std::os::unix::fs::symlink;

        let root = temp_dir("resource-symlink");
        let executable = root.join("vibapp/bin/vibapp-launcher");
        let resources = root.join("vibapp/resources/helpers");
        fs::create_dir_all(executable.parent().unwrap()).unwrap();
        fs::create_dir_all(&resources).unwrap();
        fs::write(&executable, b"launcher").unwrap();
        let outside = root.join("outside.py");
        fs::write(&outside, b"print('outside')\n").unwrap();
        symlink(&outside, resources.join("helper.py")).unwrap();
        let development = root.join("development.py");
        fs::write(&development, b"print('development')\n").unwrap();
        let error = resolve_resource_for(
            HostOs::Linux,
            &executable,
            Path::new("helpers/helper.py"),
            &development,
            ResourceKind::File,
        )
        .unwrap_err();
        assert!(error.contains("拒绝回退"));
        fs::remove_dir_all(root).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn packaged_resource_root_symlink_is_rejected_without_development_fallback() {
        use std::os::unix::fs::symlink;

        let root = temp_dir("resource-root-symlink");
        let executable = root.join("vibapp/bin/vibapp-launcher");
        fs::create_dir_all(executable.parent().unwrap()).unwrap();
        fs::write(&executable, b"launcher").unwrap();
        let outside = root.join("outside-resources/helpers");
        fs::create_dir_all(&outside).unwrap();
        fs::write(outside.join("helper.py"), b"print('outside')\n").unwrap();
        symlink(
            root.join("outside-resources"),
            root.join("vibapp/resources"),
        )
        .unwrap();
        let development = root.join("development.py");
        fs::write(&development, b"print('development')\n").unwrap();

        let error = resolve_resource_for(
            HostOs::Linux,
            &executable,
            Path::new("helpers/helper.py"),
            &development,
            ResourceKind::File,
        )
        .unwrap_err();
        assert!(error.contains("拒绝回退"));
        fs::remove_dir_all(root).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn packaged_resource_intermediate_symlink_is_rejected() {
        use std::os::unix::fs::symlink;

        let root = temp_dir("resource-intermediate-symlink");
        let executable = root.join("VibApp.app/Contents/MacOS/vibapp-launcher");
        fs::create_dir_all(executable.parent().unwrap()).unwrap();
        fs::write(&executable, b"launcher").unwrap();
        fs::create_dir_all(root.join("VibApp.app/Contents/Resources")).unwrap();
        let outside = root.join("outside-helpers");
        fs::create_dir_all(&outside).unwrap();
        fs::write(outside.join("helper.py"), b"print('outside')\n").unwrap();
        symlink(&outside, root.join("VibApp.app/Contents/Resources/helpers")).unwrap();
        let development = root.join("development.py");
        fs::write(&development, b"print('development')\n").unwrap();

        assert!(
            resolve_resource_for(
                HostOs::Macos,
                &executable,
                Path::new("helpers/helper.py"),
                &development,
                ResourceKind::File,
            )
            .is_err()
        );
        fs::remove_dir_all(root).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn packaged_service_runtime_ancestor_symlink_is_rejected() {
        use std::os::unix::fs::symlink;

        let root = temp_dir("service-ancestor-symlink");
        let executable = root.join("vibapp/bin/vibapp-launcher");
        fs::create_dir_all(executable.parent().unwrap()).unwrap();
        fs::write(&executable, b"launcher").unwrap();
        let outside = root.join("outside-libexec");
        fs::create_dir_all(&outside).unwrap();
        fs::write(outside.join("vibapp-service-runtime"), b"runtime").unwrap();
        symlink(&outside, root.join("vibapp/libexec")).unwrap();
        let development = root.join("development-runtime");
        fs::write(&development, b"development").unwrap();

        let error =
            resolve_service_runtime_for(HostOs::Linux, &executable, &development).unwrap_err();
        assert!(error.contains("拒绝回退"));
        fs::remove_dir_all(root).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn helper_alias_policy_accepts_only_targets_inside_the_install_domain() {
        use std::os::unix::fs::symlink;

        let root = temp_dir("helper-alias-policy");
        let install = root.join("homebrew");
        let candidate = install.join("bin/codex");
        let inside = install.join("Caskroom/codex/1.0/bin/codex");
        let outside = root.join("outside/codex");
        fs::create_dir_all(candidate.parent().unwrap()).unwrap();
        fs::create_dir_all(inside.parent().unwrap()).unwrap();
        fs::create_dir_all(outside.parent().unwrap()).unwrap();
        fs::write(&inside, b"inside").unwrap();
        fs::write(&outside, b"outside").unwrap();

        symlink(&inside, &candidate).unwrap();
        assert_eq!(
            resolve_trusted_tool_candidate(&candidate, &install),
            Some(fs::canonicalize(&inside).unwrap())
        );
        fs::remove_file(&candidate).unwrap();
        symlink(&outside, &candidate).unwrap();
        assert!(resolve_trusted_tool_candidate(&candidate, &install).is_none());
        fs::remove_dir_all(root).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn python_candidate_policy_rejects_a_symlinked_bin_ancestor() {
        use std::os::unix::fs::symlink;

        let root = temp_dir("python-ancestor-policy");
        let install = root.join("homebrew");
        let real_bin = install.join("real-bin");
        fs::create_dir_all(&real_bin).unwrap();
        fs::write(real_bin.join("python3"), b"python").unwrap();
        symlink(&real_bin, install.join("bin")).unwrap();

        assert!(resolve_trusted_tool_candidate(&install.join("bin/python3"), &install).is_none());
        fs::remove_dir_all(root).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn python_candidate_policy_rejects_an_alias_outside_the_install_domain() {
        use std::os::unix::fs::symlink;

        let root = temp_dir("python-alias-escape");
        let install = root.join("homebrew");
        let candidate = install.join("bin/python3");
        let outside = root.join("outside/python3");
        fs::create_dir_all(candidate.parent().unwrap()).unwrap();
        fs::create_dir_all(outside.parent().unwrap()).unwrap();
        fs::write(&outside, b"python").unwrap();
        symlink(&outside, &candidate).unwrap();

        assert!(resolve_trusted_tool_candidate(&candidate, &install).is_none());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn daemon_transport_selection_is_explicit_and_windows_is_not_executable() {
        let data = Path::new("/var/lib/vibapp");
        assert_eq!(
            daemon_transport_policy_for(HostOs::Linux, data),
            DaemonTransportPolicy::UnixOwnerSocket {
                path: data.join("runtime-daemon/run/vibappd.sock")
            }
        );
        assert_eq!(
            daemon_transport_policy_for(HostOs::Windows, data),
            DaemonTransportPolicy::WindowsOwnerNamedPipeRequired {
                pipe_name: r"\\.\pipe\vibappd-owner-v1-{owner-sid-sha256}".to_string()
            }
        );
    }

    #[test]
    fn python_and_helper_discovery_never_use_ambient_path_on_windows() {
        assert!(python_candidates(HostOs::Windows).is_empty());
        assert!(trusted_helper_candidates(HostOs::Windows, "codex").is_empty());
        assert!(
            python_candidates(HostOs::Linux)
                .iter()
                .all(|path| path.starts_with('/'))
        );
        assert!(
            trusted_helper_candidates(HostOs::Macos, "codex")
                .iter()
                .all(|path| path.is_absolute())
        );
    }

    #[test]
    fn secret_store_policy_requires_windows_owner_acl() {
        assert_eq!(
            secret_protection_policy_for(HostOs::Linux),
            SecretProtectionPolicy::UnixOwnerMode
        );
        assert_eq!(
            secret_protection_policy_for(HostOs::Windows),
            SecretProtectionPolicy::WindowsOwnerAclRequired
        );
    }

    #[cfg(unix)]
    #[test]
    fn unix_secret_file_must_be_owner_only_before_read() {
        let root = temp_dir("secret-mode");
        let secret = root.join("secret.json");
        fs::write(&secret, b"{}\n").unwrap();
        fs::set_permissions(&secret, fs::Permissions::from_mode(0o644)).unwrap();
        assert!(verify_private_file(&secret).is_err());
        protect_private_file(&secret).unwrap();
        verify_private_file(&secret).unwrap();
        assert_eq!(
            fs::metadata(&secret).unwrap().permissions().mode() & 0o777,
            0o600
        );
        fs::remove_dir_all(root).unwrap();
    }
}
