//! Application document drafting and one-page Helvetica PDF rendering.

mod assets;
mod contacts;
mod drafting;
mod fallback;
mod fields;
mod helvetica;
mod keywords;
mod layout;
mod letter;
mod markdown;
mod pdf;
mod resume;
mod selection;
mod suggestions;
mod workflow;

pub use assets::{
    AssetFiles, asset_filenames, resolve_cover_letter_base, resolve_cover_letter_text, safe_job_id,
};
pub use contacts::lookup_contacts;
pub use fallback::ContactLine;
pub use markdown::{clean, strip_inline};
pub use pdf::{Rendered, render, render_resume_template};
pub use selection::{PreparedProfile, ProfileScope, Selection, prepare_profile};
pub use suggestions::{SuggestRequest, suggest_selection};
pub use workflow::{
    GenerateOptions, GenerateRequest, Generated, GenerationError, corpus_hydration_id, generate,
    hydrate_description,
};
