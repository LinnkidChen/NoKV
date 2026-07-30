use std::cmp::Ordering;
use std::collections::{BTreeMap, BTreeSet};

use serde_json::{Number, Value};

const ANNOTATION_KEYWORDS: &[&str] = &[
    "$comment",
    "default",
    "deprecated",
    "description",
    "example",
    "examples",
    "readOnly",
    "title",
    "writeOnly",
];

#[derive(Clone, Debug)]
pub struct CompiledInputSchema {
    root: CompiledSchema,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SchemaCompilationError {
    pub keyword: String,
    pub schema_path: String,
    pub message: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SchemaViolation {
    pub keyword: &'static str,
    pub instance_path: String,
    pub schema_path: String,
    pub message: String,
}

#[derive(Clone, Debug)]
enum CompiledSchema {
    Boolean { accepts: bool, schema_path: String },
    Node(Box<SchemaNode>),
}

#[derive(Clone, Debug, Default)]
struct SchemaNode {
    schema_path: String,
    accepted_types: Option<Vec<JsonType>>,
    enum_values: Option<Vec<Value>>,
    properties: BTreeMap<String, CompiledSchema>,
    required: BTreeSet<String>,
    additional_properties: bool,
    items: Option<CompiledSchema>,
    any_of: Vec<CompiledSchema>,
    one_of: Vec<CompiledSchema>,
    minimum: Option<i64>,
    maximum: Option<i64>,
    min_length: Option<usize>,
    max_length: Option<usize>,
    max_items: Option<usize>,
    max_properties: Option<usize>,
    pattern: Option<CompiledPattern>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum JsonType {
    Object,
    Array,
    String,
    Integer,
    Number,
    Boolean,
    Null,
}

#[derive(Clone, Debug)]
enum CompiledPattern {
    AnchoredLiteralLowerHex { prefix: String, digits: usize },
}

impl CompiledInputSchema {
    pub fn compile(schema: &Value) -> Result<Self, SchemaCompilationError> {
        Ok(Self {
            root: compile_schema(schema, "")?,
        })
    }

    pub fn validate(&self, instance: &Value) -> Result<(), SchemaViolation> {
        self.root.validate_at(instance, "")
    }
}

#[cfg(test)]
pub fn string_pattern_witness(schema: &Value) -> Option<String> {
    let pattern = schema.get("pattern")?.as_str()?;
    CompiledPattern::compile(pattern, "")
        .ok()
        .map(|compiled| match compiled {
            CompiledPattern::AnchoredLiteralLowerHex { prefix, digits } => {
                format!("{prefix}{}", "a".repeat(digits))
            }
        })
}

impl CompiledSchema {
    fn validate_at(&self, instance: &Value, instance_path: &str) -> Result<(), SchemaViolation> {
        match self {
            Self::Boolean { accepts: true, .. } => Ok(()),
            Self::Boolean {
                accepts: false,
                schema_path,
            } => Err(SchemaViolation {
                keyword: "falseSchema",
                instance_path: instance_path.to_owned(),
                schema_path: schema_path.clone(),
                message: "the schema rejects every value".to_owned(),
            }),
            Self::Node(node) => node.validate_at(instance, instance_path),
        }
    }
}

impl SchemaNode {
    fn validate_at(&self, instance: &Value, instance_path: &str) -> Result<(), SchemaViolation> {
        if let Some(accepted_types) = &self.accepted_types {
            if !accepted_types
                .iter()
                .any(|accepted| accepted.matches(instance))
            {
                let expected = accepted_types
                    .iter()
                    .map(|accepted| accepted.label())
                    .collect::<Vec<_>>()
                    .join("|");
                return Err(self.violation(
                    "type",
                    instance_path,
                    format!("expected {expected}, got {}", value_type_label(instance)),
                ));
            }
        }

        if let Some(enum_values) = &self.enum_values {
            if !enum_values.iter().any(|candidate| candidate == instance) {
                return Err(self.violation(
                    "enum",
                    instance_path,
                    "value is not in the advertised enum".to_owned(),
                ));
            }
        }

        if let Some(object) = instance.as_object() {
            if !self.additional_properties {
                let mut unknown = object
                    .keys()
                    .filter(|name| !self.properties.contains_key(*name))
                    .collect::<Vec<_>>();
                unknown.sort();
                if let Some(name) = unknown.first() {
                    return Err(SchemaViolation {
                        keyword: "additionalProperties",
                        instance_path: pointer_child(instance_path, name),
                        schema_path: pointer_child(&self.schema_path, "additionalProperties"),
                        message: format!("property {name} is not allowed"),
                    });
                }
            }

            for name in &self.required {
                if !object.contains_key(name) {
                    return Err(self.violation(
                        "required",
                        instance_path,
                        format!("required property {name} is missing"),
                    ));
                }
            }

            if let Some(max_properties) = self.max_properties {
                if object.len() > max_properties {
                    return Err(self.violation(
                        "maxProperties",
                        instance_path,
                        format!(
                            "object has {} properties, maximum is {max_properties}",
                            object.len()
                        ),
                    ));
                }
            }

            for (name, schema) in &self.properties {
                if let Some(value) = object.get(name) {
                    schema.validate_at(value, &pointer_child(instance_path, name))?;
                }
            }
        }

        if let Some(array) = instance.as_array() {
            if let Some(max_items) = self.max_items {
                if array.len() > max_items {
                    return Err(self.violation(
                        "maxItems",
                        instance_path,
                        format!("array has {} items, maximum is {max_items}", array.len()),
                    ));
                }
            }
            if let Some(items) = &self.items {
                for (index, value) in array.iter().enumerate() {
                    items.validate_at(value, &pointer_child(instance_path, &index.to_string()))?;
                }
            }
        }

        if let Some(value) = instance.as_str() {
            let character_count = value.chars().count();
            if let Some(min_length) = self.min_length {
                if character_count < min_length {
                    return Err(self.violation(
                        "minLength",
                        instance_path,
                        format!("string has {character_count} characters, minimum is {min_length}"),
                    ));
                }
            }
            if let Some(max_length) = self.max_length {
                if character_count > max_length {
                    return Err(self.violation(
                        "maxLength",
                        instance_path,
                        format!("string has {character_count} characters, maximum is {max_length}"),
                    ));
                }
            }
            if let Some(pattern) = &self.pattern {
                if !pattern.matches(value) {
                    return Err(self.violation(
                        "pattern",
                        instance_path,
                        "string does not match the advertised pattern".to_owned(),
                    ));
                }
            }
        }

        if let Value::Number(number) = instance {
            if let Some(minimum) = self.minimum {
                if compare_integer_number(number, minimum) == Some(Ordering::Less) {
                    return Err(self.violation(
                        "minimum",
                        instance_path,
                        format!("number is below minimum {minimum}"),
                    ));
                }
            }
            if let Some(maximum) = self.maximum {
                if compare_integer_number(number, maximum) == Some(Ordering::Greater) {
                    return Err(self.violation(
                        "maximum",
                        instance_path,
                        format!("number is above maximum {maximum}"),
                    ));
                }
            }
        }

        if !self.any_of.is_empty()
            && !self
                .any_of
                .iter()
                .any(|schema| schema.validate_at(instance, instance_path).is_ok())
        {
            return Err(self.violation(
                "anyOf",
                instance_path,
                "value does not match any advertised branch".to_owned(),
            ));
        }

        if !self.one_of.is_empty() {
            let matches = self
                .one_of
                .iter()
                .filter(|schema| schema.validate_at(instance, instance_path).is_ok())
                .count();
            if matches != 1 {
                return Err(self.violation(
                    "oneOf",
                    instance_path,
                    format!("value matches {matches} branches; exactly one is required"),
                ));
            }
        }

        Ok(())
    }

    fn violation(
        &self,
        keyword: &'static str,
        instance_path: &str,
        message: String,
    ) -> SchemaViolation {
        SchemaViolation {
            keyword,
            instance_path: instance_path.to_owned(),
            schema_path: pointer_child(&self.schema_path, keyword),
            message,
        }
    }
}

impl JsonType {
    fn parse(value: &str, schema_path: &str) -> Result<Self, SchemaCompilationError> {
        match value {
            "object" => Ok(Self::Object),
            "array" => Ok(Self::Array),
            "string" => Ok(Self::String),
            "integer" => Ok(Self::Integer),
            "number" => Ok(Self::Number),
            "boolean" => Ok(Self::Boolean),
            "null" => Ok(Self::Null),
            other => Err(compilation_error(
                "type",
                schema_path,
                format!("unsupported JSON type {other}"),
            )),
        }
    }

    fn matches(self, value: &Value) -> bool {
        match self {
            Self::Object => value.is_object(),
            Self::Array => value.is_array(),
            Self::String => value.is_string(),
            // Workbench's integer consumers use serde_json's lossless integer
            // accessors. Reject float-backed values here as well so validation
            // cannot claim acceptance that the provider then rejects, and so
            // f64 rounding cannot move a decimal value across an integer bound.
            Self::Integer => value.as_i64().is_some() || value.as_u64().is_some(),
            Self::Number => value.is_number(),
            Self::Boolean => value.is_boolean(),
            Self::Null => value.is_null(),
        }
    }

    fn label(self) -> &'static str {
        match self {
            Self::Object => "object",
            Self::Array => "array",
            Self::String => "string",
            Self::Integer => "integer",
            Self::Number => "number",
            Self::Boolean => "boolean",
            Self::Null => "null",
        }
    }
}

impl CompiledPattern {
    fn compile(pattern: &str, schema_path: &str) -> Result<Self, SchemaCompilationError> {
        const LOWER_HEX_REPEAT: &str = "[0-9a-f]{";

        let Some(body) = pattern
            .strip_prefix('^')
            .and_then(|body| body.strip_suffix('$'))
        else {
            return Err(compilation_error(
                "pattern",
                schema_path,
                "pattern must be anchored with ^ and $".to_owned(),
            ));
        };
        let Some((prefix, repeat)) = body.split_once(LOWER_HEX_REPEAT) else {
            return Err(compilation_error(
                "pattern",
                schema_path,
                "supported pattern grammar is ^<literal>[0-9a-f]{N}$".to_owned(),
            ));
        };
        let Some(digits) = repeat
            .strip_suffix('}')
            .and_then(|digits| digits.parse::<usize>().ok())
            .filter(|digits| *digits > 0)
        else {
            return Err(compilation_error(
                "pattern",
                schema_path,
                "lower-hex repetition must be a positive integer".to_owned(),
            ));
        };
        if !prefix
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b':' | b'_' | b'-' | b'/'))
        {
            return Err(compilation_error(
                "pattern",
                schema_path,
                "pattern literal contains an unsupported regex metacharacter".to_owned(),
            ));
        }
        Ok(Self::AnchoredLiteralLowerHex {
            prefix: prefix.to_owned(),
            digits,
        })
    }

    fn matches(&self, value: &str) -> bool {
        match self {
            Self::AnchoredLiteralLowerHex { prefix, digits } => {
                let Some(hex) = value.strip_prefix(prefix) else {
                    return false;
                };
                hex.len() == *digits
                    && hex
                        .bytes()
                        .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
            }
        }
    }
}

fn compile_schema(
    schema: &Value,
    schema_path: &str,
) -> Result<CompiledSchema, SchemaCompilationError> {
    if let Some(accepts) = schema.as_bool() {
        return Ok(CompiledSchema::Boolean {
            accepts,
            schema_path: schema_path.to_owned(),
        });
    }
    let object = schema.as_object().ok_or_else(|| {
        compilation_error(
            "schema",
            schema_path,
            "schema must be an object or boolean".to_owned(),
        )
    })?;

    for keyword in object.keys() {
        match keyword.as_str() {
            "additionalProperties"
            | "anyOf"
            | "enum"
            | "items"
            | "maxItems"
            | "maxLength"
            | "maxProperties"
            | "maximum"
            | "minLength"
            | "minimum"
            | "oneOf"
            | "pattern"
            | "properties"
            | "required"
            | "type" => {}
            annotation if ANNOTATION_KEYWORDS.contains(&annotation) => {}
            unsupported => {
                return Err(compilation_error(
                    unsupported,
                    &pointer_child(schema_path, unsupported),
                    format!("unsupported schema keyword {unsupported}"),
                ));
            }
        }
    }

    if let Some(description) = object.get("description") {
        if !description.is_string() {
            return Err(compilation_error(
                "description",
                &pointer_child(schema_path, "description"),
                "description must be a string".to_owned(),
            ));
        }
    }

    let mut node = SchemaNode {
        schema_path: schema_path.to_owned(),
        additional_properties: true,
        ..SchemaNode::default()
    };

    if let Some(value) = object.get("type") {
        node.accepted_types = Some(compile_types(value, &pointer_child(schema_path, "type"))?);
    }

    if let Some(value) = object.get("enum") {
        let values = value.as_array().ok_or_else(|| {
            compilation_error(
                "enum",
                &pointer_child(schema_path, "enum"),
                "enum must be an array".to_owned(),
            )
        })?;
        if values.is_empty() {
            return Err(compilation_error(
                "enum",
                &pointer_child(schema_path, "enum"),
                "enum must contain at least one value".to_owned(),
            ));
        }
        for (index, candidate) in values.iter().enumerate() {
            let candidate_path =
                pointer_child(&pointer_child(schema_path, "enum"), &index.to_string());
            if !matches!(candidate, Value::Null | Value::Bool(_) | Value::String(_)) {
                return Err(compilation_error(
                    "enum",
                    &candidate_path,
                    "this limited compiler supports only string, boolean, and null enum values"
                        .to_owned(),
                ));
            }
            if values[..index].contains(candidate) {
                return Err(compilation_error(
                    "enum",
                    &candidate_path,
                    "enum values must be unique".to_owned(),
                ));
            }
        }
        node.enum_values = Some(values.clone());
    }

    if let Some(value) = object.get("properties") {
        let properties = value.as_object().ok_or_else(|| {
            compilation_error(
                "properties",
                &pointer_child(schema_path, "properties"),
                "properties must be an object".to_owned(),
            )
        })?;
        for (name, property_schema) in properties {
            let property_path =
                pointer_child(&pointer_child(schema_path, "properties"), name.as_str());
            node.properties.insert(
                name.clone(),
                compile_schema(property_schema, &property_path)?,
            );
        }
    }

    if let Some(value) = object.get("required") {
        let required = value.as_array().ok_or_else(|| {
            compilation_error(
                "required",
                &pointer_child(schema_path, "required"),
                "required must be an array".to_owned(),
            )
        })?;
        for (index, name) in required.iter().enumerate() {
            let name = name.as_str().ok_or_else(|| {
                compilation_error(
                    "required",
                    &pointer_child(&pointer_child(schema_path, "required"), &index.to_string()),
                    "required entries must be strings".to_owned(),
                )
            })?;
            if !node.required.insert(name.to_owned()) {
                return Err(compilation_error(
                    "required",
                    &pointer_child(schema_path, "required"),
                    format!("required contains duplicate property {name}"),
                ));
            }
        }
    }

    if let Some(value) = object.get("additionalProperties") {
        node.additional_properties = value.as_bool().ok_or_else(|| {
            compilation_error(
                "additionalProperties",
                &pointer_child(schema_path, "additionalProperties"),
                "additionalProperties must be a boolean".to_owned(),
            )
        })?;
    }

    if let Some(value) = object.get("items") {
        node.items = Some(compile_schema(value, &pointer_child(schema_path, "items"))?);
    }

    node.any_of = compile_schema_array(object.get("anyOf"), schema_path, "anyOf")?;
    node.one_of = compile_schema_array(object.get("oneOf"), schema_path, "oneOf")?;
    node.minimum = compile_integer_bound(object.get("minimum"), schema_path, "minimum")?;
    node.maximum = compile_integer_bound(object.get("maximum"), schema_path, "maximum")?;
    if node.minimum.is_some() || node.maximum.is_some() {
        let supports_bounded_integer = node.accepted_types.as_ref().is_some_and(|types| {
            types.contains(&JsonType::Integer)
                && types
                    .iter()
                    .all(|value_type| matches!(value_type, JsonType::Integer | JsonType::Null))
        });
        if !supports_bounded_integer {
            let keyword = if node.minimum.is_some() {
                "minimum"
            } else {
                "maximum"
            };
            return Err(compilation_error(
                keyword,
                &pointer_child(schema_path, keyword),
                "numeric bounds are supported only for integer or integer|null schemas".to_owned(),
            ));
        }
    }
    node.min_length = compile_nonnegative_usize(object.get("minLength"), schema_path, "minLength")?;
    node.max_length = compile_nonnegative_usize(object.get("maxLength"), schema_path, "maxLength")?;
    node.max_items = compile_nonnegative_usize(object.get("maxItems"), schema_path, "maxItems")?;
    node.max_properties =
        compile_nonnegative_usize(object.get("maxProperties"), schema_path, "maxProperties")?;

    if let Some(value) = object.get("pattern") {
        let pattern = value.as_str().ok_or_else(|| {
            compilation_error(
                "pattern",
                &pointer_child(schema_path, "pattern"),
                "pattern must be a string".to_owned(),
            )
        })?;
        node.pattern = Some(CompiledPattern::compile(
            pattern,
            &pointer_child(schema_path, "pattern"),
        )?);
    }

    Ok(CompiledSchema::Node(Box::new(node)))
}

fn compile_types(
    value: &Value,
    schema_path: &str,
) -> Result<Vec<JsonType>, SchemaCompilationError> {
    match value {
        Value::String(raw) => Ok(vec![JsonType::parse(raw, schema_path)?]),
        Value::Array(raw) if !raw.is_empty() => {
            let mut types = Vec::with_capacity(raw.len());
            for (index, item) in raw.iter().enumerate() {
                let item_path = pointer_child(schema_path, &index.to_string());
                let raw = item.as_str().ok_or_else(|| {
                    compilation_error(
                        "type",
                        &item_path,
                        "type array entries must be strings".to_owned(),
                    )
                })?;
                let parsed = JsonType::parse(raw, &item_path)?;
                if types.contains(&parsed) {
                    return Err(compilation_error(
                        "type",
                        &item_path,
                        format!("type contains duplicate value {raw}"),
                    ));
                }
                types.push(parsed);
            }
            Ok(types)
        }
        Value::Array(_) => Err(compilation_error(
            "type",
            schema_path,
            "type array must not be empty".to_owned(),
        )),
        _ => Err(compilation_error(
            "type",
            schema_path,
            "type must be a string or non-empty string array".to_owned(),
        )),
    }
}

fn compile_schema_array(
    value: Option<&Value>,
    schema_path: &str,
    keyword: &'static str,
) -> Result<Vec<CompiledSchema>, SchemaCompilationError> {
    let Some(value) = value else {
        return Ok(Vec::new());
    };
    let branches = value.as_array().ok_or_else(|| {
        compilation_error(
            keyword,
            &pointer_child(schema_path, keyword),
            format!("{keyword} must be an array"),
        )
    })?;
    if branches.is_empty() {
        return Err(compilation_error(
            keyword,
            &pointer_child(schema_path, keyword),
            format!("{keyword} must contain at least one schema"),
        ));
    }
    branches
        .iter()
        .enumerate()
        .map(|(index, branch)| {
            compile_schema(
                branch,
                &pointer_child(&pointer_child(schema_path, keyword), &index.to_string()),
            )
        })
        .collect()
}

fn compile_integer_bound(
    value: Option<&Value>,
    schema_path: &str,
    keyword: &'static str,
) -> Result<Option<i64>, SchemaCompilationError> {
    value
        .map(|value| {
            value
                .as_i64()
                .or_else(|| value.as_u64().and_then(|number| i64::try_from(number).ok()))
                .ok_or_else(|| {
                    compilation_error(
                        keyword,
                        &pointer_child(schema_path, keyword),
                        format!("{keyword} must be an integer in the signed 64-bit range"),
                    )
                })
        })
        .transpose()
}

fn compare_integer_number(number: &Number, bound: i64) -> Option<Ordering> {
    if let Some(value) = number.as_i64() {
        return Some(value.cmp(&bound));
    }
    number.as_u64().map(|value| {
        if bound < 0 {
            Ordering::Greater
        } else {
            value.cmp(&(bound as u64))
        }
    })
}

fn compile_nonnegative_usize(
    value: Option<&Value>,
    schema_path: &str,
    keyword: &'static str,
) -> Result<Option<usize>, SchemaCompilationError> {
    value
        .map(|value| {
            let number = value.as_u64().ok_or_else(|| {
                compilation_error(
                    keyword,
                    &pointer_child(schema_path, keyword),
                    format!("{keyword} must be a non-negative integer"),
                )
            })?;
            usize::try_from(number).map_err(|_| {
                compilation_error(
                    keyword,
                    &pointer_child(schema_path, keyword),
                    format!("{keyword} is too large for this platform"),
                )
            })
        })
        .transpose()
}

fn compilation_error(keyword: &str, schema_path: &str, message: String) -> SchemaCompilationError {
    SchemaCompilationError {
        keyword: keyword.to_owned(),
        schema_path: schema_path.to_owned(),
        message,
    }
}

fn pointer_child(parent: &str, component: &str) -> String {
    let escaped = component.replace('~', "~0").replace('/', "~1");
    format!("{parent}/{escaped}")
}

fn value_type_label(value: &Value) -> &'static str {
    match value {
        Value::Null => "null",
        Value::Bool(_) => "boolean",
        Value::Number(_) => "number",
        Value::String(_) => "string",
        Value::Array(_) => "array",
        Value::Object(_) => "object",
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    #[test]
    fn validates_nested_closed_and_intentionally_open_objects() {
        let schema = CompiledInputSchema::compile(&json!({
            "type": "object",
            "required": ["closed", "open"],
            "properties": {
                "closed": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "additionalProperties": false
                },
                "open": {"type": "object"}
            },
            "additionalProperties": false
        }))
        .unwrap();

        schema
            .validate(&json!({
                "closed": {"name": "ok"},
                "open": {"caller": {"defined": true}}
            }))
            .unwrap();
        let violation = schema
            .validate(&json!({
                "closed": {"name": "ok", "unknown": true},
                "open": {}
            }))
            .unwrap_err();
        assert_eq!(violation.keyword, "additionalProperties");
        assert_eq!(violation.instance_path, "/closed/unknown");
        assert_eq!(
            violation.schema_path,
            "/properties/closed/additionalProperties"
        );
    }

    #[test]
    fn validates_types_enums_unions_and_one_of() {
        let schema = CompiledInputSchema::compile(&json!({
            "type": "object",
            "required": ["section", "value"],
            "properties": {
                "section": {
                    "type": ["string", "null"],
                    "enum": ["input", "outputs", null]
                },
                "value": {
                    "anyOf": [
                        {"type": "integer", "minimum": 0},
                        {"type": "boolean"}
                    ]
                },
                "snapshot_id": {"type": "integer", "minimum": 0},
                "name": {"type": "string", "minLength": 1}
            },
            "oneOf": [
                {"required": ["snapshot_id"]},
                {"required": ["name"]}
            ],
            "additionalProperties": false
        }))
        .unwrap();

        schema
            .validate(&json!({
                "section": null,
                "value": 0,
                "snapshot_id": 1
            }))
            .unwrap();
        assert_eq!(
            schema
                .validate(&json!({
                    "section": "input",
                    "value": "not-a-number-or-boolean",
                    "snapshot_id": 1
                }))
                .unwrap_err()
                .keyword,
            "anyOf"
        );
        assert_eq!(
            schema
                .validate(&json!({
                    "section": "invalid",
                    "value": true,
                    "name": "checkpoint"
                }))
                .unwrap_err()
                .keyword,
            "enum"
        );
        assert_eq!(
            schema
                .validate(&json!({
                    "section": "input",
                    "value": -1,
                    "name": "checkpoint"
                }))
                .unwrap_err()
                .keyword,
            "anyOf"
        );
        assert_eq!(
            schema
                .validate(&json!({
                    "section": "input",
                    "value": false,
                    "snapshot_id": 1,
                    "name": "checkpoint"
                }))
                .unwrap_err()
                .keyword,
            "oneOf"
        );
    }

    #[test]
    fn validates_arrays_bounds_lengths_properties_and_pattern() {
        let schema = CompiledInputSchema::compile(&json!({
            "type": "object",
            "required": ["digest", "items", "metadata"],
            "properties": {
                "digest": {
                    "type": "string",
                    "minLength": 71,
                    "maxLength": 71,
                    "pattern": "^sha256:[0-9a-f]{64}$"
                },
                "items": {
                    "type": "array",
                    "maxItems": 2,
                    "items": {"type": "integer", "minimum": 1, "maximum": 3}
                },
                "metadata": {
                    "type": "object",
                    "maxProperties": 1
                }
            },
            "additionalProperties": false
        }))
        .unwrap();
        let valid_digest = format!("sha256:{}", "a".repeat(64));
        schema
            .validate(&json!({
                "digest": valid_digest,
                "items": [1, 3],
                "metadata": {"owner": "caller"}
            }))
            .unwrap();
        assert_eq!(
            schema
                .validate(&json!({
                    "digest": format!("sha256:{}", "A".repeat(64)),
                    "items": [],
                    "metadata": {}
                }))
                .unwrap_err()
                .keyword,
            "pattern"
        );
        assert_eq!(
            schema
                .validate(&json!({
                    "digest": format!("sha256:{}", "a".repeat(64)),
                    "items": [1, 2, 3],
                    "metadata": {}
                }))
                .unwrap_err()
                .keyword,
            "maxItems"
        );
        assert_eq!(
            schema
                .validate(&json!({
                    "digest": format!("sha256:{}", "a".repeat(64)),
                    "items": [0],
                    "metadata": {}
                }))
                .unwrap_err()
                .keyword,
            "minimum"
        );
        assert_eq!(
            schema
                .validate(&json!({
                    "digest": format!("sha256:{}", "a".repeat(64)),
                    "items": [4],
                    "metadata": {}
                }))
                .unwrap_err()
                .keyword,
            "maximum"
        );
        assert_eq!(
            schema
                .validate(&json!({
                    "digest": "",
                    "items": [],
                    "metadata": {}
                }))
                .unwrap_err()
                .keyword,
            "minLength"
        );
        assert_eq!(
            schema
                .validate(&json!({
                    "digest": format!("sha256:{}", "a".repeat(65)),
                    "items": [],
                    "metadata": {}
                }))
                .unwrap_err()
                .keyword,
            "maxLength"
        );
        assert_eq!(
            schema
                .validate(&json!({
                    "digest": format!("sha256:{}", "a".repeat(64)),
                    "items": [],
                    "metadata": {"a": 1, "b": 2}
                }))
                .unwrap_err()
                .keyword,
            "maxProperties"
        );
    }

    #[test]
    fn integer_validation_is_lossless_and_bounds_do_not_use_f64() {
        let schema = CompiledInputSchema::compile(&json!({
            "type": "integer",
            "minimum": 1,
            "maximum": 9_007_199_254_740_992_i64
        }))
        .unwrap();

        schema.validate(&json!(1)).unwrap();
        assert_eq!(schema.validate(&json!(1.0)).unwrap_err().keyword, "type");
        for raw in ["0.99999999999999999", "100.000000000000001"] {
            let rounded_float: Value = serde_json::from_str(raw).unwrap();
            assert_eq!(
                schema.validate(&rounded_float).unwrap_err().keyword,
                "type",
                "{raw} must fail closed after serde_json stores it as a float"
            );
        }
        let above_exact_f64_range = json!(9_007_199_254_740_993_u64);
        assert_eq!(
            schema.validate(&above_exact_f64_range).unwrap_err().keyword,
            "maximum"
        );
    }

    #[test]
    fn compilation_fails_closed_on_unsupported_or_malformed_keywords() {
        CompiledInputSchema::compile(&json!({
            "type": "object",
            "title": "annotation",
            "default": {},
            "examples": [{}],
            "properties": {},
            "additionalProperties": false
        }))
        .expect("recognized non-assertion annotations must not block startup");

        let unsupported = CompiledInputSchema::compile(&json!({
            "type": "object",
            "unevaluatedProperties": false
        }))
        .unwrap_err();
        assert_eq!(unsupported.keyword, "unevaluatedProperties");
        assert_eq!(unsupported.schema_path, "/unevaluatedProperties");

        let malformed = CompiledInputSchema::compile(&json!({
            "type": "object",
            "maxItems": -1
        }))
        .unwrap_err();
        assert_eq!(malformed.keyword, "maxItems");
        assert_eq!(malformed.schema_path, "/maxItems");

        let numeric_enum = CompiledInputSchema::compile(&json!({"enum": [1]})).unwrap_err();
        assert_eq!(numeric_enum.keyword, "enum");
        assert_eq!(numeric_enum.schema_path, "/enum/0");

        let duplicate_enum =
            CompiledInputSchema::compile(&json!({"enum": ["x", "x"]})).unwrap_err();
        assert_eq!(duplicate_enum.keyword, "enum");
        assert_eq!(duplicate_enum.schema_path, "/enum/1");

        let unsupported_type =
            CompiledInputSchema::compile(&json!({"type": ["string", "decimal"]})).unwrap_err();
        assert_eq!(unsupported_type.keyword, "type");
        assert_eq!(unsupported_type.schema_path, "/type/1");

        let generic_hex_pattern = CompiledInputSchema::compile(&json!({
            "type": "string",
            "pattern": "^blake3:[0-9a-f]{8}$"
        }))
        .unwrap();
        generic_hex_pattern
            .validate(&json!("blake3:0123abcd"))
            .unwrap();
        assert_eq!(
            generic_hex_pattern
                .validate(&json!("blake3:0123abcD"))
                .unwrap_err()
                .keyword,
            "pattern"
        );
        let unsupported_pattern = CompiledInputSchema::compile(&json!({
            "type": "string",
            "pattern": "^.*$"
        }))
        .unwrap_err();
        assert_eq!(unsupported_pattern.keyword, "pattern");
    }
}
