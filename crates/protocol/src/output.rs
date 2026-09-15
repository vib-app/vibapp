//! Host-side structural checks for bounded WIT UI views.

use std::collections::{BTreeMap, BTreeSet};

use crate::manifest::ProtocolErrorCode;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ViewNode {
    pub id: String,
    pub parent: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ViewShape {
    pub root: String,
    pub nodes: Vec<ViewNode>,
}

/// Validate identity, root, parent, reachability, and cycle invariants before rendering.
pub fn validate_view_shape(view: &ViewShape) -> Result<(), ProtocolErrorCode> {
    if view.root.is_empty() || view.nodes.is_empty() {
        return Err(ProtocolErrorCode::MalformedOutput);
    }
    let mut parents = BTreeMap::new();
    for node in &view.nodes {
        if node.id.is_empty()
            || parents
                .insert(node.id.as_str(), node.parent.as_deref())
                .is_some()
        {
            return Err(ProtocolErrorCode::MalformedOutput);
        }
    }
    if parents.get(view.root.as_str()) != Some(&None) {
        return Err(ProtocolErrorCode::MalformedOutput);
    }
    if parents
        .iter()
        .any(|(id, parent)| *id != view.root && parent.is_none())
    {
        return Err(ProtocolErrorCode::MalformedOutput);
    }
    for (id, parent) in &parents {
        if let Some(parent_id) = *parent {
            if parent_id == *id || !parents.contains_key(parent_id) {
                return Err(ProtocolErrorCode::MalformedOutput);
            }
        }
    }
    for id in parents.keys() {
        let mut current = Some(*id);
        let mut seen = BTreeSet::new();
        while let Some(candidate) = current {
            if !seen.insert(candidate) {
                return Err(ProtocolErrorCode::MalformedOutput);
            }
            current = parents.get(candidate).and_then(|parent| *parent);
        }
        if !seen.contains(view.root.as_str()) {
            return Err(ProtocolErrorCode::MalformedOutput);
        }
    }
    Ok(())
}
