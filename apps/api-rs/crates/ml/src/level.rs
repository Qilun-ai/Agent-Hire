//! Input layouts for the level encoder's four training versions.

use std::sync::LazyLock;

use agent_hire_jobindex as jobindex;
use regex::{Regex, RegexBuilder};

use crate::Job;
use crate::text;

static REQUIREMENTS: LazyLock<Regex> = LazyLock::new(|| {
    heading(concat!(
        r"(?:minimum|basic|required|key|essential|mandatory)[\s\x1c-\x1f]+(?:qualifications|requirements|skills)|",
        r"qualifications|requirements|required skills|skills (?:and|&) (?:experience|qualifications)|",
        r"what you(?:'ll)? (?:need|bring)|what we(?:'re)? looking for|who you are|you have|the person|",
        r"about you|your profile|profile required|votre profil|ihr profil|profil|requisitos|requisitos y experiencia|",
        r"qualifica[cç][oõ]es|you're our person if|eligibility|must have(?: skills)?|",
        r"任职要求|任职资格|岗位要求|职位要求|应聘条件|招聘要求|応募要件|応募資格|必須(?:要件|条件|スキル)?|자격 ?요건|지원 ?자격",
    ))
});
static ROLE: LazyLock<Regex> = LazyLock::new(|| {
    heading(concat!(
        r"responsibilities|key responsibilities|what you(?:'ll)? do|the role|about the role|role description|job description|",
        r"your role|position overview|岗位职责|职位描述|工作职责|業務内容|仕事内容|담당 ?업무",
    ))
});
static STOP: LazyLock<Regex> = LazyLock::new(|| {
    insensitive(concat!(
        r"^[^\p{L}\p{N}_]*(?:about (?:us|the company|[A-Zİı][\p{L}\p{N}_&.\- ]{1,30})$|",
        r"benefits|what we offer|why (?:join|work)|equal (?:employment )?opportunit|#li|salary|",
        r"compensation|pay range|the base salary|disclaimer|福利|待遇|公司介绍)",
    ))
});
static NUMBERED: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^[\s\x1c-\x1f]*(?:1[、.．)]|①)").expect("the numbered-list regex must compile")
});
static INLINE_REQUIREMENTS: LazyLock<Regex> = LazyLock::new(|| {
    insensitive(concat!(
        r"(?:minimum|basic|required|preferred) qualifications|qualifications\s*:|",
        r"requirements\s*:|what you(?:'ll)? need\s*:",
    ))
});

fn heading(alternatives: &str) -> Regex {
    // All alternatives end in a word character. Consume the boundary when a suffix
    // follows so the punctuation or space still counts toward the 40-character limit.
    insensitive(&format!(
        r"^[^\p{{L}}\p{{N}}_]*(?:{alternatives})(?:$|[^\p{{L}}\p{{N}}_\n][^\n]{{0,39}}$)"
    ))
}

fn insensitive(pattern: &str) -> Regex {
    // Python re.I also treats dotted and dotless I as variants of ASCII i.
    RegexBuilder::new(&pattern.replace('i', "[iİı]"))
        .case_insensitive(true)
        .build()
        .expect("the level header regex must compile")
}

fn excerpt(description: &str, chars: usize) -> String {
    let lines: Vec<&str> = description
        .split(['\r', '\n'])
        .map(|part| part.trim_matches(text::is_space))
        .filter(|part| !part.is_empty())
        .collect();
    let required = lines
        .iter()
        .position(|line| line.chars().count() <= 90 && REQUIREMENTS.is_match(line));
    let numbered = || {
        // A second item numbered 1 can mark a new requirements list.
        let mut starts = lines
            .iter()
            .enumerate()
            .filter(|(_, line)| NUMBERED.is_match(line));
        starts.nth(1).map(|(index, _)| index)
    };
    let start = match required.or_else(numbered) {
        Some(start) => start,
        None => {
            let flat = text::collapse_whitespace(description);
            if let Some(header) = INLINE_REQUIREMENTS.find(&flat) {
                return text::prefix(&flat[header.start()..], chars).to_owned();
            }
            let role = lines
                .iter()
                .position(|line| line.chars().count() <= 90 && ROLE.is_match(line));
            role.unwrap_or_else(|| usize::from(lines.len() > 3 && lines[0].chars().count() > 200))
        }
    };

    let mut selected = Vec::new();
    let mut length = 0;
    for line in &lines[start..] {
        if !selected.is_empty() && STOP.is_match(line) {
            break;
        }
        selected.push(*line);
        length += line.chars().count() + 1;
        if length >= chars {
            break;
        }
    }
    let joined = selected.join(" ");
    let collapsed = text::collapse_whitespace(&joined);
    text::prefix(collapsed.trim_matches(text::is_space), chars).to_owned()
}

pub(crate) fn build_text(job: &Job<'_>, version: i64, separator: &str) -> String {
    let result = jobindex::classify_level(job.title, job.description);
    let experience: Vec<&str> = result
        .lines
        .iter()
        .take(4)
        .map(|line| text::prefix(line, 260))
        .collect();
    let excerpt = match version {
        4.. => excerpt(job.description, 900),
        3 => excerpt(job.description, 400),
        2 if experience.is_empty() => excerpt(job.description, 600),
        _ if experience.is_empty() => {
            let flat = text::collapse_whitespace(job.description);
            text::prefix(flat.trim_matches(text::is_space), 450).to_owned()
        }
        _ => String::new(),
    };
    text::join_fields(
        [
            text::prefix(job.title, 160),
            &experience.join(" | "),
            &excerpt,
        ],
        separator,
    )
}

#[cfg(test)]
mod tests {
    use crate::Job;

    use super::{build_text, excerpt};

    #[test]
    fn selects_excerpts_in_python_order() {
        assert_eq!(
            excerpt(
                "Intro\nResponsibilities\nBuild tools\nRequired Skills:\nUse Rust\nBenefits\nEquity",
                600
            ),
            "Required Skills: Use Rust"
        );
        assert_eq!(
            excerpt(
                "1、开发软件\n2、维护软件\n①掌握编程\n2、独立工作\n待遇\n奖金",
                600
            ),
            "①掌握编程 2、独立工作"
        );
        assert_eq!(
            excerpt(
                "Intro\nThe Role\nSome text. Required Qualifications, Capabilities, and Skills\nBenefits\nPerks",
                43
            ),
            "Required Qualifications, Capabilities, and "
        );
        assert_eq!(
            excerpt("Intro\n仕事内 容\n仕事内容\nテスト\n福利", 600),
            "仕事内容 テスト"
        );
        assert_eq!(
            excerpt(&format!("{}\nSecond\nThird\nFourth", "é".repeat(201)), 600),
            "Second Third Fourth"
        );
    }

    #[test]
    fn keeps_experience_with_the_right_input_versions() {
        let job = Job {
            title: "Backend Engineer",
            description: "Company intro\nRequirements:\n3+ years of professional experience with Rust.\nBenefits\nInsurance",
            ..Job::default()
        };
        let evidence = "Backend Engineer <eos> 3+ years of professional experience with Rust.";
        for version in [1, 2] {
            assert_eq!(
                build_text(&job, version, "<eos>"),
                evidence,
                "version {version}"
            );
        }
        for version in [3, 4, 5] {
            assert_eq!(
                build_text(&job, version, "<eos>"),
                format!(
                    "{evidence} <eos> Requirements: 3+ years of professional experience with Rust."
                ),
                "version {version}"
            );
        }
    }

    #[test]
    fn applies_character_limits_and_omits_empty_fields() {
        let title = "👩".repeat(161);
        let description = format!("Intro\nRequirements:\n{}", "é".repeat(1000));
        let job = Job {
            title: &title,
            description: &description,
            ..Job::default()
        };
        for (version, expected_start, limit) in [
            (1, "Intro Requirements:", 450),
            (2, "Requirements:", 600),
            (3, "Requirements:", 400),
            (4, "Requirements:", 900),
        ] {
            let output = build_text(&job, version, "<eos>");
            let (title, excerpt) = output.split_once(" <eos> ").expect("expected two fields");
            assert_eq!(title.chars().count(), 160, "version {version}");
            assert_eq!(excerpt.chars().count(), limit, "version {version}");
            assert!(
                excerpt.starts_with(expected_start),
                "version {version}: {excerpt}"
            );
        }
        assert_eq!(build_text(&Job::default(), 4, "<eos>"), "");
    }

    #[test]
    fn keeps_only_the_first_four_experience_lines() {
        let description: String = (2..=6)
            .map(|years| format!("{years}+ years of professional experience with Rust.\n"))
            .collect();
        let job = Job {
            description: &description,
            ..Job::default()
        };
        let evidence = build_text(&job, 1, "|");
        let lines: Vec<&str> = evidence.split(" | ").collect();
        assert_eq!(lines.len(), 4);
        assert!(lines[0].starts_with("2+"), "{evidence}");
        assert!(lines[3].starts_with("5+"), "{evidence}");
    }
}
