//! Deployment requests and uploads for the two Kaggle notebook services.

use std::fs;
use std::path::{Component, Path, PathBuf};

use base64::Engine as _;
use base64::engine::general_purpose::STANDARD;
use serde_json::{Map, Value, json};

use crate::ModelDirectories;
use crate::jupyter::Jupyter;
use crate::kaggle::KaggleError;
use crate::kaggle_code;

const REMOTE_REL: &str = "agent-hire-decision";
const REMOTE_ROOT: &str = "/kaggle/working";
const DECISION_SERVICE: &str = include_str!("../data/kaggle_remote/remote_service.py");
const CLASSIFIER_SERVICE: &str = include_str!("../data/kaggle_remote/classifier_service.py");

pub(crate) fn decision(
    client: &mut Jupyter,
    key: &str,
    deployment: &str,
) -> Result<(), KaggleError> {
    let config = json!({"deployment_id":deployment,"api_key":key,"domain":"","ngrok_token":"",
                        "replicas":1,"port":8073,"max_inflight":16});
    let metadata = json!({"service":STANDARD.encode(DECISION_SERVICE),"config":config});
    client.last_json(kaggle_code::START_CODE, 180, &metadata)?;
    Ok(())
}

pub(crate) fn classifiers(
    client: &mut Jupyter,
    directories: &ModelDirectories,
    key: &str,
    deployment: &str,
) -> Result<bool, KaggleError> {
    let mut files = Vec::new();
    let mut models = Map::new();
    for (name, directory) in [
        ("department", &directories.department),
        ("level", &directories.level),
    ] {
        let labels = directory.join("labels.json");
        if !labels.exists() {
            continue;
        }
        let metadata = fs::read(&labels).map_err(KaggleError::new)?;
        let metadata: Value = serde_json::from_slice(&metadata).map_err(KaggleError::new)?;
        let model = metadata["model_file"]
            .as_str()
            .filter(|name| !name.is_empty())
            .unwrap_or("model_w4.onnx");
        let path = Path::new(model);
        if path.file_name().is_none()
            || !path
                .components()
                .all(|component| matches!(component, Component::Normal(_) | Component::CurDir))
        {
            return Err(KaggleError::new("Invalid encoder model file name"));
        }
        let destination = format!("{REMOTE_REL}/models/{name}");
        for file in ["labels.json", "tokenizer.json", model] {
            files.push(Upload {
                local: directory.join(file),
                relative: format!("{destination}/{file}"),
                absolute: format!("{REMOTE_ROOT}/{destination}/{file}"),
            });
        }
        models.insert(
            name.to_owned(),
            json!(format!("{REMOTE_ROOT}/{destination}")),
        );
    }
    if files.is_empty() {
        return Ok(false);
    }
    let paths: Vec<&str> = files.iter().map(|file| file.absolute.as_str()).collect();
    let sizes = client.last_json(kaggle_code::SIZES_CODE, 60, &json!({"paths":paths}))?;
    for file in files {
        let size = file.local.metadata().map_err(KaggleError::new)?.len();
        if sizes[&file.absolute].as_u64() != Some(size) {
            client.upload(&file.local, &file.relative)?;
        }
    }
    let config = json!({"port":8074,"key":key,"deployment_id":deployment,"models":models});
    let metadata = json!({"service":STANDARD.encode(CLASSIFIER_SERVICE),"config":config});
    client.last_json(kaggle_code::CLASSIFIER_START_CODE, 120, &metadata)?;
    Ok(true)
}

struct Upload {
    local: PathBuf,
    relative: String,
    absolute: String,
}
