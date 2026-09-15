/// ABI version consumed by the browser bridge before it accepts the module.
#[unsafe(no_mangle)]
pub extern "C" fn vibapp_web_abi_version() -> u32 {
    1
}

/// The same bounded first-pass validation used by the web Launcher composer.
#[unsafe(no_mangle)]
pub extern "C" fn vibapp_accept_need(character_count: u32) -> u32 {
    u32::from((10..=2_000).contains(&character_count))
}

/// Returns 0=ui, 1=service, 2=hybrid from bounded host-derived signals.
#[unsafe(no_mangle)]
pub extern "C" fn vibapp_classify_kind(ui_signal: u32, service_signal: u32) -> u32 {
    match (ui_signal != 0, service_signal != 0) {
        (true, true) => 2,
        (false, true) => 1,
        _ => 0,
    }
}

/// Browser execution is always foreground-only in this product contract.
#[unsafe(no_mangle)]
pub extern "C" fn vibapp_browser_background_reliability() -> u32 {
    0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn need_bounds_are_closed() {
        assert_eq!(vibapp_accept_need(9), 0);
        assert_eq!(vibapp_accept_need(10), 1);
        assert_eq!(vibapp_accept_need(2_000), 1);
        assert_eq!(vibapp_accept_need(2_001), 0);
    }

    #[test]
    fn kind_classification_is_deterministic() {
        assert_eq!(vibapp_classify_kind(1, 0), 0);
        assert_eq!(vibapp_classify_kind(0, 1), 1);
        assert_eq!(vibapp_classify_kind(1, 1), 2);
        assert_eq!(vibapp_classify_kind(0, 0), 0);
    }
}
