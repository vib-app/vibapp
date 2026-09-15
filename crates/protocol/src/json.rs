//! Dependency-free strict JSON parsing used before package admission.

use std::cmp::Ordering;
use std::fmt;

/// A JSON value. Objects retain their input order; canonical serialization sorts keys.
#[derive(Clone, Debug)]
pub enum JsonValue {
    Null,
    Bool(bool),
    Integer(i64),
    String(String),
    Array(Vec<JsonValue>),
    Object(Vec<(String, JsonValue)>),
}

impl PartialEq for JsonValue {
    fn eq(&self, other: &Self) -> bool {
        match (self, other) {
            (Self::Null, Self::Null) => true,
            (Self::Bool(left), Self::Bool(right)) => left == right,
            (Self::Integer(left), Self::Integer(right)) => left == right,
            (Self::String(left), Self::String(right)) => left == right,
            (Self::Array(left), Self::Array(right)) => left == right,
            (Self::Object(left), Self::Object(right)) => object_entries_equal(left, right),
            _ => false,
        }
    }
}

impl Eq for JsonValue {}

fn object_entries_equal(left: &[(String, JsonValue)], right: &[(String, JsonValue)]) -> bool {
    if left.len() != right.len() {
        return false;
    }

    let mut matched = vec![false; right.len()];
    for (left_key, left_value) in left {
        let Some(index) = right
            .iter()
            .enumerate()
            .position(|(index, (right_key, right_value))| {
                !matched[index] && left_key == right_key && left_value == right_value
            })
        else {
            return false;
        };
        matched[index] = true;
    }
    true
}

impl JsonValue {
    pub fn get(&self, key: &str) -> Option<&Self> {
        match self {
            Self::Object(entries) => entries
                .iter()
                .find_map(|(candidate, value)| (candidate == key).then_some(value)),
            _ => None,
        }
    }

    pub fn as_object(&self) -> Option<&[(String, JsonValue)]> {
        match self {
            Self::Object(value) => Some(value),
            _ => None,
        }
    }

    pub fn as_array(&self) -> Option<&[JsonValue]> {
        match self {
            Self::Array(value) => Some(value),
            _ => None,
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            Self::String(value) => Some(value),
            _ => None,
        }
    }

    pub fn as_i64(&self) -> Option<i64> {
        match self {
            Self::Integer(value) => Some(*value),
            _ => None,
        }
    }

    pub fn as_bool(&self) -> Option<bool> {
        match self {
            Self::Bool(value) => Some(*value),
            _ => None,
        }
    }

    /// Serialize using the deterministic JSON subset required by Stage 0.
    ///
    /// Object keys use RFC 8785 / JCS UTF-16 code-unit ordering. The accepted
    /// manifest schema only permits integral numbers, avoiding floating-point
    /// spelling ambiguity.
    #[must_use]
    pub fn to_canonical_json(&self) -> String {
        let mut output = String::new();
        self.write_canonical(&mut output);
        output
    }

    fn write_canonical(&self, output: &mut String) {
        match self {
            Self::Null => output.push_str("null"),
            Self::Bool(true) => output.push_str("true"),
            Self::Bool(false) => output.push_str("false"),
            Self::Integer(value) => output.push_str(&value.to_string()),
            Self::String(value) => write_string(value, output),
            Self::Array(values) => {
                output.push('[');
                for (index, value) in values.iter().enumerate() {
                    if index != 0 {
                        output.push(',');
                    }
                    value.write_canonical(output);
                }
                output.push(']');
            }
            Self::Object(entries) => {
                let mut sorted: Vec<_> = entries.iter().collect();
                sorted.sort_by(|left, right| utf16_cmp(&left.0, &right.0));
                output.push('{');
                for (index, (key, value)) in sorted.into_iter().enumerate() {
                    if index != 0 {
                        output.push(',');
                    }
                    write_string(key, output);
                    output.push(':');
                    value.write_canonical(output);
                }
                output.push('}');
            }
        }
    }
}

fn utf16_cmp(left: &str, right: &str) -> Ordering {
    left.encode_utf16().cmp(right.encode_utf16())
}

fn write_string(value: &str, output: &mut String) {
    output.push('"');
    for character in value.chars() {
        match character {
            '"' => output.push_str("\\\""),
            '\\' => output.push_str("\\\\"),
            '\u{08}' => output.push_str("\\b"),
            '\u{09}' => output.push_str("\\t"),
            '\u{0a}' => output.push_str("\\n"),
            '\u{0c}' => output.push_str("\\f"),
            '\u{0d}' => output.push_str("\\r"),
            control if control <= '\u{1f}' => {
                use std::fmt::Write as _;
                let _ = write!(output, "\\u{:04x}", u32::from(control));
            }
            other => output.push(other),
        }
    }
    output.push('"');
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum JsonErrorKind {
    UnexpectedEnd,
    UnexpectedToken,
    TrailingData,
    DuplicateKey(String),
    InvalidEscape,
    InvalidUnicode,
    InvalidNumber,
    NonIntegralNumber,
    NumberOutOfRange,
    UnescapedControl,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct JsonError {
    pub kind: JsonErrorKind,
    pub byte_offset: usize,
}

impl fmt::Display for JsonError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "JSON error at byte {}: {:?}",
            self.byte_offset, self.kind
        )
    }
}

impl std::error::Error for JsonError {}

/// Parse the strict, integral-number JSON subset accepted for Stage 0 manifests.
pub fn parse_json(input: &str) -> Result<JsonValue, JsonError> {
    let mut parser = Parser { input, offset: 0 };
    parser.skip_whitespace();
    let value = parser.parse_value()?;
    parser.skip_whitespace();
    if parser.offset != input.len() {
        return Err(parser.error(JsonErrorKind::TrailingData));
    }
    Ok(value)
}

struct Parser<'a> {
    input: &'a str,
    offset: usize,
}

impl Parser<'_> {
    fn error(&self, kind: JsonErrorKind) -> JsonError {
        JsonError {
            kind,
            byte_offset: self.offset,
        }
    }

    fn remaining(&self) -> &str {
        &self.input[self.offset..]
    }

    fn peek(&self) -> Option<char> {
        self.remaining().chars().next()
    }

    fn next(&mut self) -> Option<char> {
        let next = self.peek()?;
        self.offset += next.len_utf8();
        Some(next)
    }

    fn skip_whitespace(&mut self) {
        while matches!(self.peek(), Some(' ' | '\n' | '\r' | '\t')) {
            let _ = self.next();
        }
    }

    fn parse_value(&mut self) -> Result<JsonValue, JsonError> {
        match self.peek() {
            Some('n') => self.parse_literal("null", JsonValue::Null),
            Some('t') => self.parse_literal("true", JsonValue::Bool(true)),
            Some('f') => self.parse_literal("false", JsonValue::Bool(false)),
            Some('"') => self.parse_string().map(JsonValue::String),
            Some('[') => self.parse_array(),
            Some('{') => self.parse_object(),
            Some('-' | '0'..='9') => self.parse_integer(),
            Some(_) => Err(self.error(JsonErrorKind::UnexpectedToken)),
            None => Err(self.error(JsonErrorKind::UnexpectedEnd)),
        }
    }

    fn parse_literal(&mut self, literal: &str, value: JsonValue) -> Result<JsonValue, JsonError> {
        if self.remaining().starts_with(literal) {
            self.offset += literal.len();
            Ok(value)
        } else {
            Err(self.error(JsonErrorKind::UnexpectedToken))
        }
    }

    fn parse_array(&mut self) -> Result<JsonValue, JsonError> {
        let _ = self.next();
        self.skip_whitespace();
        let mut values = Vec::new();
        if self.peek() == Some(']') {
            let _ = self.next();
            return Ok(JsonValue::Array(values));
        }
        loop {
            self.skip_whitespace();
            values.push(self.parse_value()?);
            self.skip_whitespace();
            match self.next() {
                Some(',') => self.skip_whitespace(),
                Some(']') => return Ok(JsonValue::Array(values)),
                Some(_) => return Err(self.error(JsonErrorKind::UnexpectedToken)),
                None => return Err(self.error(JsonErrorKind::UnexpectedEnd)),
            }
        }
    }

    fn parse_object(&mut self) -> Result<JsonValue, JsonError> {
        let _ = self.next();
        self.skip_whitespace();
        let mut entries: Vec<(String, JsonValue)> = Vec::new();
        if self.peek() == Some('}') {
            let _ = self.next();
            return Ok(JsonValue::Object(entries));
        }
        loop {
            self.skip_whitespace();
            if self.peek() != Some('"') {
                return Err(self.error(JsonErrorKind::UnexpectedToken));
            }
            let key = self.parse_string()?;
            if entries.iter().any(|(existing, _)| existing == &key) {
                return Err(self.error(JsonErrorKind::DuplicateKey(key)));
            }
            self.skip_whitespace();
            if self.next() != Some(':') {
                return Err(self.error(JsonErrorKind::UnexpectedToken));
            }
            self.skip_whitespace();
            let value = self.parse_value()?;
            entries.push((key, value));
            self.skip_whitespace();
            match self.next() {
                Some(',') => self.skip_whitespace(),
                Some('}') => return Ok(JsonValue::Object(entries)),
                Some(_) => return Err(self.error(JsonErrorKind::UnexpectedToken)),
                None => return Err(self.error(JsonErrorKind::UnexpectedEnd)),
            }
        }
    }

    fn parse_integer(&mut self) -> Result<JsonValue, JsonError> {
        let start = self.offset;
        if self.peek() == Some('-') {
            let _ = self.next();
        }
        match self.peek() {
            Some('0') => {
                let _ = self.next();
                if matches!(self.peek(), Some('0'..='9')) {
                    return Err(self.error(JsonErrorKind::InvalidNumber));
                }
            }
            Some('1'..='9') => {
                while matches!(self.peek(), Some('0'..='9')) {
                    let _ = self.next();
                }
            }
            _ => return Err(self.error(JsonErrorKind::InvalidNumber)),
        }
        if matches!(self.peek(), Some('.' | 'e' | 'E')) {
            return Err(self.error(JsonErrorKind::NonIntegralNumber));
        }
        self.input[start..self.offset]
            .parse::<i64>()
            .map(JsonValue::Integer)
            .map_err(|_| self.error(JsonErrorKind::NumberOutOfRange))
    }

    fn parse_string(&mut self) -> Result<String, JsonError> {
        if self.next() != Some('"') {
            return Err(self.error(JsonErrorKind::UnexpectedToken));
        }
        let mut output = String::new();
        loop {
            let character = self
                .next()
                .ok_or_else(|| self.error(JsonErrorKind::UnexpectedEnd))?;
            match character {
                '"' => return Ok(output),
                '\\' => self.parse_escape(&mut output)?,
                control if control <= '\u{1f}' => {
                    return Err(self.error(JsonErrorKind::UnescapedControl));
                }
                other => output.push(other),
            }
        }
    }

    fn parse_escape(&mut self, output: &mut String) -> Result<(), JsonError> {
        match self.next() {
            Some('"') => output.push('"'),
            Some('\\') => output.push('\\'),
            Some('/') => output.push('/'),
            Some('b') => output.push('\u{08}'),
            Some('f') => output.push('\u{0c}'),
            Some('n') => output.push('\n'),
            Some('r') => output.push('\r'),
            Some('t') => output.push('\t'),
            Some('u') => {
                let first = self.parse_hex_quad()?;
                let scalar = if (0xd800..=0xdbff).contains(&first) {
                    if self.next() != Some('\\') || self.next() != Some('u') {
                        return Err(self.error(JsonErrorKind::InvalidUnicode));
                    }
                    let second = self.parse_hex_quad()?;
                    if !(0xdc00..=0xdfff).contains(&second) {
                        return Err(self.error(JsonErrorKind::InvalidUnicode));
                    }
                    0x1_0000 + ((u32::from(first) - 0xd800) << 10) + (u32::from(second) - 0xdc00)
                } else if (0xdc00..=0xdfff).contains(&first) {
                    return Err(self.error(JsonErrorKind::InvalidUnicode));
                } else {
                    u32::from(first)
                };
                output.push(
                    char::from_u32(scalar)
                        .ok_or_else(|| self.error(JsonErrorKind::InvalidUnicode))?,
                );
            }
            Some(_) => return Err(self.error(JsonErrorKind::InvalidEscape)),
            None => return Err(self.error(JsonErrorKind::UnexpectedEnd)),
        }
        Ok(())
    }

    fn parse_hex_quad(&mut self) -> Result<u16, JsonError> {
        let mut value = 0_u16;
        for _ in 0..4 {
            let digit = self
                .next()
                .and_then(|character| character.to_digit(16))
                .ok_or_else(|| self.error(JsonErrorKind::InvalidUnicode))?;
            value = value * 16
                + u16::try_from(digit).map_err(|_| self.error(JsonErrorKind::InvalidUnicode))?;
        }
        Ok(value)
    }
}
