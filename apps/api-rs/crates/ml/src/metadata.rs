//! Model configuration from the exporter-supplied labels file.

use std::fs;
use std::path::Path;
use std::str::FromStr;

use serde::{Deserialize, Deserializer};
use serde_json::Value;

use crate::Error;
use crate::text;

#[derive(Clone, Copy, Debug, Deserialize)]
#[serde(default)]
pub(crate) struct Cuts {
    pub(crate) title: isize,
    pub(crate) department: isize,
    pub(crate) company: isize,
    pub(crate) excerpt: isize,
}

impl Default for Cuts {
    fn default() -> Self {
        Self {
            title: 160,
            department: 80,
            company: 60,
            excerpt: 700,
        }
    }
}

#[derive(Debug, Deserialize)]
pub(crate) struct Metadata {
    pub(crate) labels: Vec<String>,
    sep: Option<String>,
    model_file: Option<String>,
    cuts: Option<Cuts>,
    #[serde(default = "default_max_len", deserialize_with = "model_integer")]
    pub(crate) max_len: usize,
    #[serde(default, deserialize_with = "model_integer")]
    pub(crate) pad_id: u32,
    #[serde(default = "default_input_version", deserialize_with = "model_integer")]
    pub(crate) input_version: i64,
}

impl Metadata {
    pub(crate) fn read(directory: &Path) -> Result<Self, Error> {
        let path = directory.join("labels.json");
        let contents = fs::read(&path).map_err(|source| Error::Read {
            path: path.clone(),
            source,
        })?;
        let metadata: Self =
            serde_json::from_slice(&contents).map_err(|source| Error::Metadata { path, source })?;
        if metadata.labels.is_empty() || metadata.max_len == 0 {
            return Err(Error::InvalidModel("labels and max_len must be nonempty"));
        }
        Ok(metadata)
    }

    pub(crate) fn separator(&self) -> &str {
        self.sep
            .as_deref()
            .filter(|sep| !sep.is_empty())
            .unwrap_or("|")
    }

    pub(crate) fn model_file(&self) -> Result<&Path, Error> {
        let name = self
            .model_file
            .as_deref()
            .filter(|name| !name.is_empty())
            .unwrap_or("model.onnx");
        Path::new(name)
            .file_name()
            .map(Path::new)
            .ok_or(Error::InvalidModel("model_file must have a file name"))
    }

    pub(crate) fn cuts(&self) -> Cuts {
        self.cuts.unwrap_or_default()
    }
}

const fn default_max_len() -> usize {
    320
}

const fn default_input_version() -> i64 {
    1
}

fn model_integer<'de, D, T>(deserializer: D) -> Result<T, D::Error>
where
    D: Deserializer<'de>,
    T: FromStr + TryFrom<i64>,
{
    use serde::de::Error as _;

    let out_of_range = || D::Error::custom("integer setting out of range");
    // Read through `Value` rather than an untagged enum: the server enables serde_json's
    // `arbitrary_precision`, under which untagged enums cannot match numbers.
    // Python calls int() on these settings, including when the exporter writes a string.
    match Value::deserialize(deserializer)? {
        Value::Number(number) => {
            if let Some(value) = number.as_i64() {
                T::try_from(value).map_err(|_| out_of_range())
            } else {
                match number.as_f64() {
                    Some(value) if (i64::MIN as f64..-(i64::MIN as f64)).contains(&value) => {
                        T::try_from(value as i64).map_err(|_| out_of_range())
                    }
                    _ => Err(out_of_range()),
                }
            }
        }
        Value::String(value) => value
            .trim_matches(text::is_space)
            .parse()
            .map_err(|_| D::Error::custom("invalid integer setting")),
        Value::Bool(value) => T::try_from(i64::from(value)).map_err(|_| out_of_range()),
        Value::Null | Value::Array(_) | Value::Object(_) => Err(D::Error::custom(
            "integer setting must be a number, string or boolean",
        )),
    }
}

#[cfg(test)]
mod tests {
    use std::path::Path;

    use super::Metadata;

    #[test]
    fn reads_defaults_and_exporter_overrides() {
        let defaults: Metadata = serde_json::from_str(r#"{"labels":["mid"],"sep":"","cuts":null}"#)
            .expect("metadata with labels uses exporter defaults");
        assert_eq!(defaults.separator(), "|");
        assert_eq!(
            defaults.model_file().expect("default file name"),
            Path::new("model.onnx")
        );
        assert_eq!(defaults.max_len, 320);
        assert_eq!(defaults.pad_id, 0);
        assert_eq!(defaults.input_version, 1);
        assert_eq!(defaults.cuts().excerpt, 700);

        let overrides: Metadata = serde_json::from_str(
            r#"{
                "labels":["mid"], "sep":"<eos>", "model_file":"export/model_w4.onnx",
                "max_len":"384", "pad_id":1, "input_version":3.5, "cuts":{"excerpt":900}
            }"#,
        )
        .expect("metadata accepts exporter overrides");
        assert_eq!(overrides.separator(), "<eos>");
        assert_eq!(
            overrides.model_file().expect("model file name"),
            Path::new("model_w4.onnx")
        );
        assert_eq!(overrides.max_len, 384);
        assert_eq!(overrides.pad_id, 1);
        assert_eq!(overrides.input_version, 3);
        assert_eq!(overrides.cuts().title, 160);
        assert_eq!(overrides.cuts().excerpt, 900);
    }
}
