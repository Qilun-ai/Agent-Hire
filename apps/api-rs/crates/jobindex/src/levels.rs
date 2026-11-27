//! Experience level from a job's title and the lines that state required experience.
//!
//! No model: a deterministic extractor for "N years", "N+ yrs", "N-M years", "at least N years",
//! word numbers, months and fresher / new-grad / entry-level wording, plus title signals, measured
//! against ~29.5k labelled postings. Levels: intern, fresher (no experience), junior (1-2 y),
//! mid (3-4 y), senior (5+ y), lead.

use std::sync::LazyLock;

use fancy_regex::{Captures, Regex};

use crate::text;

fn re(pattern: &str) -> Regex {
    Regex::new(pattern).expect("level patterns are valid")
}

fn ci(pattern: &str) -> Regex {
    re(&format!("(?i){pattern}"))
}

fn found(regex: &Regex, haystack: &str) -> bool {
    regex.is_match(haystack).unwrap_or(false)
}

/// Whether `regex` matches at the very start of `haystack` (Python's `re.match`).
fn matches_at_start(regex: &Regex, haystack: &str) -> bool {
    matches!(regex.find(haystack), Ok(Some(m)) if m.start() == 0)
}

const NUM: &str = r"(?:\d{1,2}(?:\.\d)?|zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty)";
const UNIT: &str = r"(?:years?|yrs?|yr|años?|anos?|ans|jahren?|anni|lat)";

static YEARS: LazyLock<Regex> = LazyLock::new(|| {
    ci(&format!(
        concat!(
            r"(?P<pre>at\s+least|minimum(?:\s+of)?|min\.?|over|more\s+than|in\s+excess\s+of|up\s+to|less\s+than|under|",
            r"maximum(?:\s+of)?|max\.?|between|entre|zwischen|mindestens|al\s+menos|m[ií]nimo(?:\s+de)?|au\s+moins|almeno|pelo\s+menos|",
            r"plus\s+de|m[aá]s\s+de|mais\s+de|über)?\s*",
            r"(?<![\d.,])(?P<a>{num})\s*(?:\(\s*\d{{1,2}}\s*\)\s*)?(?P<plus>\+|plus)?\s*",
            r"(?:(?:-|–|—|to|or|and|y|e|und|et|a|à|bis)\s*(?P<b>{num})\s*(?P<plus2>\+)?\s*)?",
            r"(?P<ormore_pre>or\s+more\s+|or\s+greater\s+|\+\s*)?",
            r"(?P<unit>{unit})(?P<or_more>\s*(?:or\s+more|or\s+above|and\s+above|\+))?"
        ),
        num = NUM,
        unit = UNIT
    ))
});
static ASIAN_YEARS: LazyLock<Regex> = LazyLock::new(|| {
    re(concat!(
        r"(?<![\d.])(?P<a>\d{1,2})\s*(?:[-~～〜至到]\s*(?P<b>\d{1,2})\s*)?",
        r"(?:(?P<cjk>年)\s*(?P<more>以上|及以上|或以上|\+)?|(?P<ko>년)\s*(?P<more_ko>이상)?|(?P<vi>năm)(?P<more_vi>\s*trở lên)?)"
    ))
});
static ASIAN_EXPERIENCE: LazyLock<Regex> = LazyLock::new(|| {
    re(r"经验|經驗|経験|工作|从事|職務|実務|営業|经历|경력|경험|kinh nghiệm|làm việc")
});
static ASIAN_PREFERRED: LazyLock<Regex> =
    LazyLock::new(|| re(r"优先|優先|尚可|歓迎|望ましい|가산|우대|ưu tiên|Preferred|preferred"));
static ASIAN_NOT_EXPERIENCE: LazyLock<Regex> = LazyLock::new(|| {
    re(
        r"成立|创立|創業|設立|历史|歴史|周年|週年|年度|年届|届|年毕业|年卒|年入社|年春|年秋|年月|thành lập",
    )
});
static NO_UNIT: LazyLock<Regex> = LazyLock::new(|| {
    ci(r"(?P<a>\d{1,2})\s*(?:-|–|to)\s*(?P<b>\d{1,2})\s*\+?\s+of\s+(?:\w+\s+){0,2}experience")
});
static MONTHS: LazyLock<Regex> = LazyLock::new(|| {
    ci(&format!(
        r"(?P<a>{NUM})\s*(?:-|–|to)?\s*(?P<b>{NUM})?\s*\+?\s*months?(?:\W+\w+){{0,4}}?\W+(?:of\s+)?(?:\w+\s+){{0,3}}experience"
    ))
});
static EXPERIENCE_WORDS: LazyLock<Regex> = LazyLock::new(|| {
    ci(concat!(
        r"experiencia|experiência|expérience|erfahrung|esperienza|doświadczeni|experience|experienced|exp\b|work(?:ing)?\b|worked|industry|professional|hands[- ]on|track record|",
        r"background|relevant|proven|building|developing|designing|managing|leading|working|career|practice|",
        r"in (?:a|the|software|engineering|data|product|sales|related)"
    ))
});
static NOT_EXPERIENCE: LazyLock<Regex> = LazyLock::new(|| {
    ci(concat!(
        r"years?\s+(?:old|of age|warranty|vesting)|year[- ]old|founded|since \d{4}|for over \d+ years,? (?:we|our|the company)|",
        r"(?:we|company|our team|firm|organi[sz]ation)\s+(?:has|have)\s+(?:been|over|more)|",
        r"\d[- ]year (?:degree|program|programme|course|contract|commitment|fixed|bond|anniversary)|",
        r"(?:degree|bachelor|master|diploma)\W[^.\n]{0,25}\(\s*\d\s*years?\s*\)|",
        r"years? (?:in business|of history|of service to|of growth)|celebrat|anniversar|",
        r"(?:contract|assignment|project|engagement|duration|tenure|term)\s+(?:of|for|is)\s+\d+\s*(?:years?|months?)|",
        r"\d+\s*(?:years?|months?)\s+(?:contract|assignment|fixed[- ]term)|",
        r"within \d+ years? of (?:joining|hire)|after \d+ years?|every (?:\d+|two|three|five) years?|per year|a year\b(?! of)|",
        r"years?\s+(?:or\s+more\s+)?of\s+(?:employment\s+|residential\s+|address\s+)?history|investigations? (?:will|may) (?:account|cover|include)|background (?:check|screening|investigation)|",
        r"(?:next|coming|past|last|first)\s+(?:\d+|few|two|three|five|ten)\s+years|",
        r"(?:has|have|had)\s+(?:been\s+)?(?:worked|working|served|operated|partnered|been)\b[^.\n]{0,50}\bfor\s+(?:over\s+|more than\s+)?\d+\s+years|",
        r"(?:over|more than)\s+\d+\s+years\s+(?:of\s+)?(?:history|heritage|legacy|excellence|innovation|serving|in business)|",
        r"\b(?:we|our|she|he|they|his|her|the team|founders?|ceo)\b[^.\n]{0,30}\b(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty)\s+years|",
        r"with\s+(?:over|more than|nearly|almost)?\s*\**\s*\d+\s*\**\s+years\s+of\s+experience,?\s+(?:we|our|the company)|",
        r"\bin\s+(?:just|only|less than)\s+\w+\s+years|",
        r"(?:has|have|had)\s+been\s+\w+ing\b[^.\n]{0,90}\bfor\s+(?:over\s+|more than\s+|nearly\s+|almost\s+)?\d+\s+years|",
        r"\d+\+?[- ]?years?\s+(?:legacy|track record|history|heritage|of (?:legacy|innovation|history|heritage|excellence|track record))|",
        r"(?:two|four|2|4)[- ]or[- ](?:two|four|2|4)[- ]year (?:college|university|degree|institution)|enrolled in|",
        r"able to (?:work|commit)|available (?:to work )?for|commit(?:ment)? (?:to|for)|willing to (?:work|stay|commit)|",
        r"over\s+\d+\s+months,?\s+you|during the first|in your first|first\s+(?:\d+|six|three|twelve)\s+months"
    ))
});
static GRAD_WINDOW: LazyLock<Regex> = LazyLock::new(|| {
    ci(concat!(
        r"(?:graduat\w*|degree|passed out|batch)[^.\n]{0,40}(?:within|in) the (?:last|past)\s+(\d|one|two)\s+years?|",
        r"(?:20\d\d|class of 20\d\d)\s+(?:graduates?|batch|pass[- ]?outs?)"
    ))
});
static FRESHER: LazyLock<Regex> = LazyLock::new(|| {
    ci(concat!(
        r"\bfreshers?\b|\bfresh graduates?\b|\bnew[- ]grad(?:uate)?s?\b|\brecent (?:college )?graduates?\b|",
        r"\bentry[- ]level\b|\bno (?:prior |previous |work |professional )?experience (?:is )?(?:required|needed|necessary)\b|",
        r"\b0\s*(?:years?|yrs?)\b|\bzero experience\b|\bcampus (?:hire|hiring|recruit\w*)\b|\bgraduate (?:engineer|trainee|program|programme|scheme)\b|",
        r"\bearly[- ]career\b|\bstarting (?:your|their) career\b|\bjust graduated\b|校招|校园招聘|应届|新卒"
    ))
});
static NOT_FRESHER: LazyLock<Regex> = LazyLock::new(|| {
    ci(concat!(
        r"\bnot\s+(?:an?\s+)?(?:entry[- ]level|junior|suitable for freshers)|\bno\s+freshers\b|",
        r"\bfreshers?\s+(?:need not|should not|are not)|\bbeyond entry[- ]level\b"
    ))
});
static PREFERRED: LazyLock<Regex> = LazyLock::new(|| {
    ci(concat!(
        r"\bprefer\w*|nice[- ]to[- ]have|\bbonus\b|\bplus\b|\bideal(?:ly)?\b|\bdesir\w*|good to have|",
        r"\badvantage\w*|would be great|\bpluses\b|\bnot required\b"
    ))
});
static PREFERRED_HEADER: LazyLock<Regex> = LazyLock::new(|| {
    ci(concat!(
        r"^\W*(?:preferred|desired|bonus|nice[- ]to[- ]have|good[- ]to[- ]have|",
        r"(?:additional|extra|other)(?:\s+or\s+preferred)?\s+(?:qualifications|skills|requirements|experience)|",
        r"what (?:would|will) (?:make you|set you)|it would be (?:great|nice)|pluses|ideal(?:ly)?)\b"
    ))
});
static REQUIRED_HEADER: LazyLock<Regex> = LazyLock::new(|| {
    ci(concat!(
        r"^\W*(?:minimum|basic|required|requirements|must[- ]have|qualifications|what you(?:'ll)? need|",
        r"who you are|what we(?:'re)? looking for|you (?:have|bring|should have)|essential|eligibility)\b"
    ))
});
static HEADER_LINE: LazyLock<Regex> = LazyLock::new(|| re(r"^\W*[A-Z][A-Za-z '’/&-]{2,60}:?\s*$"));

static TITLE_RULES: LazyLock<Vec<(&'static str, Regex)>> = LazyLock::new(|| {
    vec![
        (
            "intern",
            ci(concat!(
                r"\b(intern(ship)?s?|co-?op|apprentice(ship)?|summer (analyst|associate)|student|werkstudent|",
                r"praktikant(in)?|praktikum|pflichtpraktikum|est[aá]gios?|estagi[aá]ri[oa]s?|pasant[ií]a|becari[oa]|tirocinio|",
                r"stagiaire|alternance|industrial trainee|externship)\b"
            )),
        ),
        (
            "fresher",
            ci(concat!(
                r"(?:校招|校园招聘|应届|新卒)|\b(fresher|freshers|graduate\b.{0,25}\b(program|programme|scheme|trainee|engineer|analyst|developer)|",
                r"new grad(uate)?|campus (hire|hiring|recruit\w*)|university grad(uate)?|early career|entry[- ]level|",
                r"graduate trainee|management trainee|trainee)\b"
            )),
        ),
        (
            "lead",
            ci(concat!(
                r"\b(staff|principal|distinguished|fellow|head of|director|managing director|executive director|",
                r"senior vice president|executive vice president|svp|evp|chief|",
                r"cto|cio|ciso|cfo|coo|ceo|lead|leader|manager|supervisor|superintendent|foreman|",
                r"werksleiter|teamleiter|leiter|gerente|responsable|chef d['e ]|jefe)\b"
            )),
        ),
        (
            "senior",
            ci(r"\b(vice president|vp|avp|assistant vice president)\b"),
        ),
        (
            "senior",
            ci(concat!(
                r"\b(senior|sr\.?|iii|iv|v|level\s*(3|4|5)|l\s?[5-7]|sde\s*-?\s*(iii|3)|swe\s*-?\s*(iii|3)|",
                r"(engineer|developer|analyst|scientist)\s*(3|4|5)|e5|e6|ic\s?[4-6])\b"
            )),
        ),
        (
            "junior",
            ci(concat!(
                r"\b(junior|jr\.?|associate|i|level\s*1|l\s?3|sde\s*-?\s*(i|1)|swe\s*-?\s*(i|1)|",
                r"(engineer|developer|analyst|scientist)\s*(1|i)|e3|ic\s?[12])\b"
            )),
        ),
        (
            "mid",
            ci(concat!(
                r"\b(ii|level\s*2|l\s?4|sde\s*-?\s*(ii|2)|swe\s*-?\s*(ii|2)|(engineer|developer|analyst|scientist)\s*(2|ii)|",
                r"mid(-?level)?|intermediate|e4|ic\s?3)\b"
            )),
        ),
    ]
});
static IC_MANAGER: LazyLock<Regex> = LazyLock::new(|| {
    ci(concat!(
        r"\b(account|product|program|programme|project|relationship|case|territory|sales|category|",
        r"community|content|success|social media|brand|partner|partnerships?|key account|campaign|",
        r"portfolio|channel|release|scrum|change|configuration|asset|wealth|client|customer)\s+manager\b|",
        r"\bmanager\b[^,]{0,3}(?:,|-|–)\s*(?:product|program|project|accounts?|sales|partnerships?)\b"
    ))
});
static INLINE_HEADER: LazyLock<Regex> = LazyLock::new(|| {
    re(concat!(
        r"\s(?=(?:Additional or preferred|Preferred/Additional|Minimum|Basic|Preferred|Required|Desired|Additional|Bonus|Nice[- ]to[- ]have|Good[- ]to[- ]have)\s+",
        r"(?:Qualifications|Requirements|Skills|Experience)\b)|\s(?=(?:Requirements|Qualifications|What you(?:'ll)? (?:need|bring))\s*:)"
    ))
});
static BULLETS: LazyLock<Regex> = LazyLock::new(|| re(r"[•●▪◦·]|\s{3,}|\s\d\)\s"));
static NEWLINES: LazyLock<Regex> = LazyLock::new(|| re(r"\n+"));
static SENTENCE_SPLIT: LazyLock<Regex> = LazyLock::new(|| re(r"(?<=[.;!?])\s+(?=[A-Z0-9(])"));
static CLAUSE_BREAK: LazyLock<Regex> = LazyLock::new(|| {
    re(concat!(
        r";|\.\s|\(|\)|,|\s[–—-]\s|\bbut\b|\bwhile\b|\bor equivalent\b|",
        r"\s+or\s+(?=[A-Z]|a\s+|an\s+|high school|bachelor|master|degree|equivalent)"
    ))
});
static GLUED: LazyLock<Regex> = LazyLock::new(|| {
    re(r"(?<=[a-z\)])(?=\d{1,2}\s*\+?\s*(?:-|–|to)?\s*\d{0,2}\s*\+?\s*(?:years|yrs))")
});
static LOST_DASH: LazyLock<Regex> =
    LazyLock::new(|| re(r"(?<![\d.,-])(\d)(\d{1,2})(?=\s*\+?\s*(?:years?|yrs?)\b)"));
static STRONG_FOLLOW: LazyLock<Regex> = LazyLock::new(|| {
    re(
        r"^\W*(?:in|of|as|at|across|within|with|doing|selling|delivering|graduate|post|bachelor|degree|[,;)]|$)",
    )
});
static GENERIC_A: LazyLock<Regex> = LazyLock::new(|| {
    re(r"^\W*(?:of\s+)?(?:\w+\s+){0,3}(?:experience|exp)\b(?!\s+(?:with|in|using|on)\b)")
});
static GENERIC_B: LazyLock<Regex> = LazyLock::new(|| {
    re(concat!(
        r"^\W*(?:of\s+)?(?:relevant|professional|industry|work|overall|total|hands[- ]on|software|engineering|",
        r"development|full[- ]time|post[- ]?graduat\w*|progressive)\b"
    ))
});
static SUB_INCLUDING: LazyLock<Regex> = LazyLock::new(|| {
    re(
        r"(?:including|incl\.?|of which|within which|out of which)\s+(?:at least|a minimum of|minimum|min\.?|over)?\s*$",
    )
});
static SUB_INCLUDING_WORD: LazyLock<Regex> = LazyLock::new(|| re(r"\b(?:including|of which)\b"));
static SUB_WITH: LazyLock<Regex> =
    LazyLock::new(|| re(r"\bwith\s+(?:at least|a minimum of|minimum|min\.?|over)?\s*$"));
static DEGREE_ALT: LazyLock<Regex> = LazyLock::new(|| {
    re(r"\b(?:degree|bachelor'?s?|master'?s?|diploma|b\.?\s?tech|be/btech)\b[^.;]{0,60}\bor\s*$")
});
static PREF_TAIL: LazyLock<Regex> =
    LazyLock::new(|| ci(r"\b(?:preferabl[ey]|ideally)\s+(?:in|with|within|from|at|on|across)\b.*"));
static FIELD_LINE: LazyLock<Regex> = LazyLock::new(|| {
    ci(
        r"^\W*(?:total\s+|overall\s+|relevant\s+|work\s+)?(?:experience|exp)\.?\s*(?:range|required|level)?\s*[:\-–]",
    )
});
static MUST_WORDS: LazyLock<Regex> = LazyLock::new(|| ci(r"\b(?:required|must|minimum)\b"));
static OPTION_LINE: LazyLock<Regex> = LazyLock::new(|| ci(r"^\W*option\s*\d"));
static OR_WORD: LazyLock<Regex> = LazyLock::new(|| ci(r"\bor\b"));
static BACHELOR: LazyLock<Regex> =
    LazyLock::new(|| ci(r"bachelor|\bb\.?\s?s\b|undergraduate|\bb\.?\s?tech"));
static STRONG_LEAD: LazyLock<Regex> = LazyLock::new(|| {
    ci(concat!(
        r"\b(staff|principal|distinguished|fellow|head of|director|vice president|vp|avp|svp|evp|chief|",
        r"cto|cio|ciso|cfo|coo|ceo|lead|leader|engineering manager)\b"
    ))
});
static PRINCIPAL_GRADE: LazyLock<Regex> =
    LazyLock::new(|| ci(r"\bprincipal\s+(associate|coordinator|analyst|assistant|clerk)\b"));
static PRINCIPAL_EXCEPT: LazyLock<Regex> =
    LazyLock::new(|| ci(r"\b(staff|lead|director|head|manager|intern)\b"));
static PRINCIPAL_EXCEPT_LEAD: LazyLock<Regex> =
    LazyLock::new(|| ci(r"\b(staff|lead|director|head|manager)\b"));
static JUNIOR_WORD: LazyLock<Regex> = LazyLock::new(|| ci(r"\b(junior|jr\.?)\b"));
static REAL_LEAD: LazyLock<Regex> = LazyLock::new(|| {
    ci(r"\b(staff|principal|lead|director|head|vp|chief|group|engineering manager)\b")
});
static STRAY_I: LazyLock<Regex> = LazyLock::new(|| ci(r"^.*\bi\b.*$"));
static LADDER_I: LazyLock<Regex> = LazyLock::new(|| {
    ci(
        r"\b(engineer|developer|analyst|scientist|associate|specialist|technician|administrator|designer|consultant)\s+i\b",
    )
});

/// One stated requirement.
#[derive(Clone, Debug, PartialEq)]
struct Mention {
    line: String,
    years_min: Option<f64>,
    years_max: Option<f64>,
    preferred: bool,
    fresher: bool,
    /// A sub-requirement ("including 2 years of X") or a degree alternative.
    sub: bool,
    /// Byte offset of the figure in its line.
    start: usize,
}

impl Mention {
    fn new(line: &str, years_min: Option<f64>, years_max: Option<f64>, preferred: bool) -> Mention {
        Mention {
            line: line.to_owned(),
            years_min,
            years_max,
            preferred,
            fresher: false,
            sub: false,
            start: 0,
        }
    }
}

/// The required experience a description states.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Experience {
    /// The bar, in years.
    pub years_min: Option<f64>,
    /// The top of a stated range.
    pub years_max: Option<f64>,
    /// Whether the posting is open to people with no experience.
    pub fresher: bool,
    /// The lines the evidence came from (at most six).
    pub lines: Vec<String>,
}

fn number_word(token: Option<&str>) -> Option<f64> {
    let token = token?.to_lowercase();
    let word = match token.as_str() {
        "zero" | "no" => Some(0.0),
        "one" | "a" | "an" => Some(1.0),
        "two" => Some(2.0),
        "three" => Some(3.0),
        "four" => Some(4.0),
        "five" => Some(5.0),
        "six" => Some(6.0),
        "seven" => Some(7.0),
        "eight" => Some(8.0),
        "nine" => Some(9.0),
        "ten" => Some(10.0),
        "eleven" => Some(11.0),
        "twelve" => Some(12.0),
        "fifteen" => Some(15.0),
        "twenty" => Some(20.0),
        _ => None,
    };
    // Python's float() reads any Unicode decimal digit ("１年以上" is one year).
    let ascii: String = token
        .chars()
        .map(|c| match c {
            '\u{ff10}'..='\u{ff19}' => char::from(b'0' + (c as u32 - 0xff10) as u8),
            '\u{0660}'..='\u{0669}' => char::from(b'0' + (c as u32 - 0x0660) as u8),
            '\u{06f0}'..='\u{06f9}' => char::from(b'0' + (c as u32 - 0x06f0) as u8),
            '\u{0966}'..='\u{096f}' => char::from(b'0' + (c as u32 - 0x0966) as u8),
            other => other,
        })
        .collect();
    word.or_else(|| ascii.parse().ok())
}

fn group<'t>(captures: &Captures<'t>, name: &str) -> Option<&'t str> {
    captures
        .name(name)
        .map(|m| m.as_str())
        .filter(|text| !text.is_empty())
}

fn lines(text: &str) -> Vec<String> {
    let text = BULLETS.replace_all(text, "\n");
    let text = INLINE_HEADER.replace_all(&text, "\n");
    let mut out = Vec::new();
    for raw in NEWLINES.split(&text).filter_map(Result::ok) {
        let raw = raw.trim();
        if raw.is_empty() {
            continue;
        }
        if raw.chars().count() > 220 {
            out.extend(
                SENTENCE_SPLIT
                    .split(raw)
                    .filter_map(Result::ok)
                    .map(str::trim)
                    .filter(|part| !part.is_empty())
                    .map(str::to_owned),
            );
        } else {
            out.push(raw.to_owned());
        }
    }
    out
}

/// The part of a line a requirement belongs to: "2+ years of JS; TypeScript preferred" → the first.
fn clause(line: &str, start: usize, end: usize) -> &str {
    let mut left = 0;
    for found in CLAUSE_BREAK
        .find_iter(&line[..start])
        .filter_map(Result::ok)
    {
        left = found.end();
    }
    let right = match CLAUSE_BREAK.find_from_pos(line, end) {
        Ok(Some(found)) => found.start(),
        _ => line.len(),
    };
    &line[left..right.max(left)]
}

/// Undoes scraping artefacts: glued sentences and dropped range dashes ("310 years" → "3-10").
fn repair(text: &str) -> String {
    let text = GLUED.replace_all(text, ". ");
    LOST_DASH
        .replace_all(&text, |captures: &Captures<'_>| {
            let low: i64 = captures[1].parse().unwrap_or(0);
            let high: i64 = captures[2].parse().unwrap_or(0);
            let whole: i64 = format!("{}{}", &captures[1], &captures[2])
                .parse()
                .unwrap_or(0);
            if whole > 25 && low < high && high <= 25 {
                format!("{low}-{high}")
            } else {
                captures[0].to_owned()
            }
        })
        .into_owned()
}

/// The required experience stated in a description, with the lines it came from.
pub fn extract_experience(text: &str) -> Experience {
    let mut mentions: Vec<Mention> = Vec::new();
    let repaired = repair(text);
    let mut section: Option<&str> = None;
    for line in lines(text::prefix(&repaired, 30_000)) {
        let short = line.chars().count() <= 80;
        let preferred_header = found(&PREFERRED_HEADER, &line);
        let required_header = found(&REQUIRED_HEADER, &line);
        if (short && (found(&HEADER_LINE, &line) || line.ends_with(':')))
            || preferred_header
            || required_header
        {
            if preferred_header {
                section = Some("preferred");
            } else if required_header {
                section = Some("required");
            } else if short {
                section = None;
            }
        }
        let line = text::prefix(&line, 2000).to_owned();
        if found(&GRAD_WINDOW, &line) {
            let mut mention = Mention::new(&line, Some(0.0), None, false);
            mention.fresher = true;
            mentions.push(mention);
            continue;
        }
        let fresher = found(&FRESHER, &line) && !found(&NOT_FRESHER, &line);
        let mut found_years = false;
        for captures in YEARS.captures_iter(&line).filter_map(Result::ok) {
            let a = number_word(group(&captures, "a"));
            let b = number_word(group(&captures, "b"));
            let Some(a) = a else { continue };
            if a > 25.0 || b.is_some_and(|b| b > 30.0 || b < a) {
                continue;
            }
            let whole = captures.get(0).expect("a match has group 0");
            let num_start = captures
                .name("pre")
                .filter(|m| !m.as_str().is_empty())
                .or_else(|| captures.name("a"))
                .map_or(whole.start(), |m| m.start());
            let window =
                &line[text::back(&line, num_start, 60)..text::forward(&line, whole.end(), 80)];
            let wider = &line[text::back(&line, num_start, 140)..whole.end()];
            if found(&NOT_EXPERIENCE, window) || found(&NOT_EXPERIENCE, wider) {
                continue;
            }
            let strong = ["plus", "plus2", "or_more", "ormore_pre", "b", "pre"]
                .iter()
                .any(|name| group(&captures, name).is_some());
            let after_words =
                line[whole.end()..text::forward(&line, whole.end(), 25)].to_lowercase();
            if !(found(&EXPERIENCE_WORDS, window)
                || (strong && matches_at_start(&STRONG_FOLLOW, &after_words)))
            {
                continue;
            }
            let pre = group(&captures, "pre").unwrap_or("").to_lowercase();
            let (mut low, mut high) = (Some(a), b);
            if ["up to", "less than", "under", "max"]
                .iter()
                .any(|prefix| pre.starts_with(prefix))
            {
                if a >= 4.0 {
                    continue; // "up to 7 years" says little about the bar; let the title decide
                }
                low = Some(0.0);
                high = Some(a);
            } else if ["plus", "or_more", "ormore_pre"]
                .iter()
                .any(|name| group(&captures, name).is_some())
                || !pre.is_empty()
            {
                high = b;
            }
            let after = line[whole.end()..text::forward(&line, whole.end(), 60)].to_lowercase();
            let _generic =
                matches_at_start(&GENERIC_A, &after) || matches_at_start(&GENERIC_B, &after);
            let this_clause = clause(&line, num_start, whole.end());
            let before = line[text::back(&line, num_start, 45)..num_start].to_lowercase();
            let sub = found(&SUB_INCLUDING, &before)
                || found(&SUB_INCLUDING_WORD, &before)
                || (found_years && found(&SUB_WITH, &before));
            let degree_alt = found(&DEGREE_ALT, &before);
            let pref_clause = PREF_TAIL.replace_all(this_clause, "");
            let field_line = matches_at_start(&FIELD_LINE, &line);
            let preferred = found(&PREFERRED, &pref_clause)
                || (section == Some("preferred")
                    && !field_line
                    && !found(&MUST_WORDS, this_clause));
            let mut mention = Mention::new(&line, low, high, preferred);
            mention.sub = sub || degree_alt;
            mention.start = num_start;
            mentions.push(mention);
            found_years = true;
        }
        if !found_years {
            for captures in ASIAN_YEARS.captures_iter(&line).filter_map(Result::ok) {
                let a = number_word(group(&captures, "a"));
                let b = number_word(group(&captures, "b"));
                let Some(a) = a else { continue };
                if a > 25.0 || b.is_some_and(|b| b > 30.0 || b < a) {
                    continue;
                }
                let whole = captures.get(0).expect("a match has group 0");
                let window = &line
                    [text::back(&line, whole.start(), 30)..text::forward(&line, whole.end(), 30)];
                let more = group(&captures, "more")
                    .or_else(|| group(&captures, "more_ko"))
                    .or_else(|| group(&captures, "more_vi"));
                if found(
                    &ASIAN_NOT_EXPERIENCE,
                    &line
                        [text::back(&line, whole.start(), 8)..text::forward(&line, whole.end(), 6)],
                ) {
                    continue;
                }
                if !(more.is_some() || b.is_some() || found(&ASIAN_EXPERIENCE, window)) {
                    continue;
                }
                let tail = &line[whole.end()..text::forward(&line, whole.end(), 40)];
                let preferred = found(
                    &ASIAN_PREFERRED,
                    &format!("{}{tail}", clause(&line, whole.start(), whole.end())),
                ) || section == Some("preferred");
                let mut mention = Mention::new(
                    &line,
                    Some(a),
                    if more.is_some() { None } else { b },
                    preferred,
                );
                mention.start = whole.start();
                mentions.push(mention);
                found_years = true;
            }
        }
        if !found_years && let Ok(Some(captures)) = NO_UNIT.captures(&line) {
            let whole = captures.get(0).expect("a match has group 0");
            if !found(&PREFERRED, clause(&line, whole.start(), whole.end())) {
                mentions.push(Mention::new(
                    &line,
                    number_word(group(&captures, "a")),
                    number_word(group(&captures, "b")),
                    false,
                ));
                found_years = true;
            }
        }
        if !found_years {
            for captures in MONTHS.captures_iter(&line).filter_map(Result::ok) {
                let whole = captures.get(0).expect("a match has group 0");
                let a = number_word(group(&captures, "a"));
                let window = &line
                    [text::back(&line, whole.start(), 60)..text::forward(&line, whole.end(), 60)];
                if let Some(a) = a
                    && a <= 24.0
                    && found(&EXPERIENCE_WORDS, window)
                    && !found(&NOT_EXPERIENCE, window)
                {
                    let years = (a / 12.0 * 10.0).round() / 10.0;
                    mentions.push(Mention::new(
                        &line,
                        Some(if years < 1.0 { 0.0 } else { years }),
                        None,
                        found(&PREFERRED, &line),
                    ));
                    found_years = true;
                    break;
                }
            }
        }
        if fresher && !found_years {
            let mut mention = Mention::new(&line, Some(0.0), None, false);
            mention.fresher = true;
            mentions.push(mention);
        }
    }
    summarize(mentions)
}

/// The bar from all mentions: per line, alternatives joined by "or" count as their minimum; the
/// job's bar is the largest remaining requirement.
fn summarize(mentions: Vec<Mention>) -> Experience {
    let mut result = Experience::default();
    let required: Vec<&Mention> = mentions
        .iter()
        .filter(|m| !m.preferred && m.years_min.is_some() && !m.fresher && !m.sub)
        .collect();
    let any_fresher = mentions.iter().any(|m| m.fresher);
    if !required.is_empty() {
        let mut per_line: Vec<(String, Vec<&Mention>)> = Vec::new();
        for mention in &required {
            match per_line.iter_mut().find(|(line, _)| *line == mention.line) {
                Some((_, group)) => group.push(mention),
                None => per_line.push((mention.line.clone(), vec![mention])),
            }
        }
        let first_min = |group: &[&Mention]| -> Mention {
            let mut best = group[0];
            for candidate in group {
                if candidate.years_min < best.years_min {
                    best = candidate;
                }
            }
            best.clone()
        };
        let mut candidates: Vec<Mention> = Vec::new();
        let mut options: Vec<&Mention> = Vec::new();
        for (line, mut group) in per_line {
            group.sort_by_key(|m| m.start);
            if matches_at_start(&OPTION_LINE, &line) {
                options.extend(group);
                continue;
            }
            let mut current: Vec<&Mention> = vec![group[0]];
            for pair in group.windows(2) {
                let (previous, next) = (pair[0], pair[1]);
                let between = &line[previous.start.min(next.start)..next.start];
                if found(&OR_WORD, between) {
                    current.push(next); // "3-5 years of X OR 5 years of Y": alternatives
                } else {
                    candidates.push(first_min(&current));
                    current = vec![next];
                }
            }
            candidates.push(first_min(&current));
        }
        if !options.is_empty() {
            let bachelor: Vec<&Mention> = options
                .iter()
                .copied()
                .filter(|m| found(&BACHELOR, &m.line))
                .collect();
            candidates.push(first_min(if bachelor.is_empty() {
                &options
            } else {
                &bachelor
            }));
        }
        let key = |m: &Mention| (m.years_min.unwrap_or(0.0), -m.years_max.unwrap_or(99.0));
        let mut best = &candidates[0];
        for candidate in &candidates {
            if key(candidate) > key(best) {
                best = candidate;
            }
        }
        result.years_min = best.years_min;
        result.years_max = best.years_max;
    } else if any_fresher {
        result.years_min = Some(0.0);
    }
    result.fresher = any_fresher && result.years_min.is_none_or(|years| years == 0.0);
    for mention in &mentions {
        if !result.lines.contains(&mention.line) {
            result.lines.push(mention.line.clone());
        }
    }
    result.lines.truncate(6);
    result
}

/// The level a title alone states, if any.
pub fn title_level(title: &str) -> Option<&'static str> {
    if found(&PRINCIPAL_GRADE, title) && !found(&PRINCIPAL_EXCEPT, title) {
        return None; // a mid-level grade: only stated years can place it
    }
    for (level, pattern) in TITLE_RULES.iter() {
        if !found(pattern, title) {
            continue;
        }
        if *level == "lead" {
            if found(&PRINCIPAL_GRADE, title) && !found(&PRINCIPAL_EXCEPT_LEAD, title) {
                continue;
            }
            if found(&JUNIOR_WORD, title) {
                continue; // "Junior Marketing Manager" is a junior role
            }
            if found(&IC_MANAGER, title) && !found(&REAL_LEAD, title) {
                continue; // "Product / Account / Project Manager" is an individual contributor
            }
        }
        if *level == "junior" && found(&STRAY_I, title) && !found(&LADDER_I, title) {
            continue; // a stray "I" is not a level
        }
        return Some(level);
    }
    None
}

fn years_level(years: f64) -> &'static str {
    if years < 1.0 {
        "fresher"
    } else if years < 3.0 {
        "junior"
    } else if years < 5.0 {
        "mid"
    } else {
        "senior"
    }
}

/// The level and the evidence for it.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct LevelResult {
    /// The level, when anything decides it.
    pub level: Option<&'static str>,
    /// The stated bar in years.
    pub years_min: Option<f64>,
    /// The top of a stated range.
    pub years_max: Option<f64>,
    /// `title` or `years`.
    pub basis: Option<&'static str>,
    /// The lines the years came from.
    pub lines: Vec<String>,
}

/// The level of a posting from its title and description.
pub fn classify_level(title: &str, description: &str) -> LevelResult {
    let experience = extract_experience(&format!("{title}\n{description}"));
    let from_title = title_level(title);
    let (level, basis) = match from_title {
        Some("intern") => (Some("intern"), Some("title")),
        // Managers count as leads by title (the "title" manager policy).
        Some("lead") => (Some("lead"), Some("title")),
        Some("fresher") if experience.years_min.is_none_or(|years| years <= 2.0) => {
            (Some("fresher"), Some("title"))
        }
        _ => match experience.years_min {
            Some(years) => (Some(years_level(years)), Some("years")),
            None => (from_title, from_title.map(|_| "title")),
        },
    };
    let _ = &STRONG_LEAD;
    LevelResult {
        level,
        years_min: experience.years_min,
        years_max: experience.years_max,
        basis,
        lines: experience.lines,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn years_and_titles() {
        assert_eq!(
            classify_level(
                "Senior Backend Engineer",
                "Go and Kafka. 5+ years of backend experience."
            )
            .level,
            Some("senior")
        );
        let result = classify_level(
            "Backend Engineer",
            "Requirements:\n3+ years of professional experience with Python.",
        );
        assert_eq!((result.level, result.years_min), (Some("mid"), Some(3.0)));
        assert_eq!(
            classify_level(
                "Software Engineer",
                "Freshers welcome! No experience required."
            )
            .level,
            Some("fresher")
        );
        assert_eq!(classify_level("Product Manager", "").level, None);
        assert_eq!(
            classify_level("Engineering Manager", "").level,
            Some("lead")
        );
        assert_eq!(
            classify_level("Data Intern", "5 years").level,
            Some("intern")
        );
        // Company history is not an experience requirement.
        assert_eq!(
            classify_level("Analyst", "We have been serving clients for over 20 years.").years_min,
            None
        );
    }

    #[test]
    fn preferred_years_do_not_set_the_bar() {
        let result = classify_level(
            "Engineer",
            "Required: 2+ years of experience in Java.\nPreferred: 6+ years of experience.",
        );
        assert_eq!(result.years_min, Some(2.0));
    }

    #[test]
    fn repairs_lost_dashes() {
        assert_eq!(
            repair("310 years of experience"),
            "3-10 years of experience"
        );
    }
}
