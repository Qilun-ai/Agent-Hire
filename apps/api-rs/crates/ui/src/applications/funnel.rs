//! Application counts and the geometry of the two-stage flow.

use maud::{Markup, html};
use serde_json::Value;

use crate::view;

#[derive(Default)]
pub(super) struct Funnel {
    pub total: u64,
    pub awaiting: u64,
    pub interviewing: u64,
    pub accepted: u64,
    pub rejected: u64,
    pub accepted_after: u64,
    pub rejected_after: u64,
}

#[derive(Clone, Copy)]
struct Stage {
    label: &'static str,
    tone: &'static str,
    value: u64,
}

struct Flow {
    from: usize,
    to: usize,
    value: u64,
}

impl Funnel {
    pub(super) fn new(counts: &Value) -> Self {
        let total = view::count(counts, "applied");
        let interviewing = view::count(counts, "interviewing").min(total);
        let accepted = view::count(counts, "accepted").min(total - interviewing);
        let rejected = view::count(counts, "rejected").min(total - interviewing - accepted);
        Self {
            total,
            awaiting: total - interviewing - accepted - rejected,
            interviewing,
            accepted,
            rejected,
            accepted_after: view::count(counts, "accepted_after_interview").min(accepted),
            rejected_after: view::count(counts, "rejected_after_interview").min(rejected),
        }
    }

    fn stages(&self) -> [Stage; 7] {
        [
            Stage {
                label: "Total applications",
                tone: "ink",
                value: self.total,
            },
            Stage {
                label: "Awaiting reply",
                tone: "warn",
                value: self.awaiting,
            },
            Stage {
                label: "Interviewing",
                tone: "teal",
                value: self.interviewing + self.accepted + self.rejected_after,
            },
            Stage {
                label: "Rejected (no interview)",
                tone: "bad",
                value: self.rejected - self.rejected_after,
            },
            Stage {
                label: "Accepted",
                tone: "ok",
                value: self.accepted,
            },
            Stage {
                label: "Rejected after interview",
                tone: "bad",
                value: self.rejected_after,
            },
            Stage {
                label: "Still interviewing",
                tone: "teal",
                value: self.interviewing,
            },
        ]
    }

    fn flows(stages: &[Stage; 7]) -> impl Iterator<Item = Flow> + '_ {
        stages
            .iter()
            .enumerate()
            .skip(1)
            .filter(|(_, stage)| stage.value > 0)
            .map(|(to, stage)| Flow {
                from: if to < 4 { 0 } else { 2 },
                to,
                value: stage.value,
            })
    }

    pub(super) fn chart(&self) -> Markup {
        let stages = self.stages();
        let scale = 132.0 / self.total.max(1) as f64;
        let xs = [30.0, 295.0, 295.0, 295.0, 570.0, 570.0, 570.0];
        let mut ys = [0.0; 7];
        for column in [&[0][..], &[1, 2, 3], &[4, 5, 6]] {
            let visible: Vec<usize> = column
                .iter()
                .copied()
                .filter(|&index| stages[index].value > 0)
                .collect();
            let size: f64 = visible
                .iter()
                .map(|&index| stages[index].value as f64 * scale)
                .sum();
            let mut y = (250.0 - size - 32.0 * visible.len().saturating_sub(1) as f64) / 2.0;
            for index in visible {
                ys[index] = y;
                y += stages[index].value as f64 * scale + 32.0;
            }
        }
        let mut outgoing = ys;
        html! {
            figure.card.flow {
                h2 { "Application funnel" }
                div.table-scroll {
                    svg.funnel viewBox="0 0 760 250" role="img" aria-label="Application counts by stage" {
                        @for flow in Self::flows(&stages) {
                            @let (x, y) = (xs[flow.from] + 13.0, outgoing[flow.from]);
                            @let (tx, ty) = (xs[flow.to], ys[flow.to]);
                            @let mid = (x + tx) / 2.0;
                            @let height = flow.value as f64 * scale;
                            @let () = { outgoing[flow.from] += height; };
                            path class=(format!("ribbon tone-{}", stages[flow.to].tone))
                                d=(format!("M{x} {y} C{mid} {y} {mid} {ty} {tx} {ty} v{height} C{mid} {} {mid} {} {x} {} Z", ty + height, y + height, y + height)) {
                                title { (stages[flow.from].label) " → " (stages[flow.to].label) " · " (view::number(flow.value)) }
                            }
                        }
                        @for (index, stage) in stages.iter().enumerate().filter(|(_, stage)| stage.value > 0) {
                            rect class=(format!("tone-{}", stage.tone)) x=(xs[index]) y=(ys[index]) width="13" height=(stage.value as f64 * scale) rx="2" {
                                title { (stage.label) " · " (view::number(stage.value)) }
                            }
                            text x=(xs[index]) y=(ys[index] - 8.0) { (stage.label) " · " (view::number(stage.value)) }
                        }
                    }
                }
                figcaption {
                    div.chips { @for (tone, label) in [("warn", "Awaiting reply"), ("teal", "Interviewing"), ("ok", "Accepted"), ("bad", "Rejected")] { span class=(format!("legend tone-{tone}")) { (label) } } }
                    p { "Follow applications from submission to their current stage." }
                    @if self.accepted > self.accepted_after {
                        p { (self.accepted - self.accepted_after) " accepted application(s) have no interview on record and are shown under Interviewing, since an offer implies one." }
                    }
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::Funnel;

    #[test]
    fn flows_conserve_counts_and_drop_empty_stages() {
        let funnel = Funnel::new(
            &json!({"applied": 17, "interviewing": 5, "accepted": 3, "rejected": 4, "accepted_after_interview": 9, "rejected_after_interview": 4}),
        );
        let stages = funnel.stages();
        assert_eq!(stages.map(|stage| stage.value), [17, 5, 12, 0, 3, 4, 5]);
        assert_eq!(funnel.accepted_after, 3);
        let flows: Vec<_> = Funnel::flows(&stages).collect();
        assert!(flows.iter().all(|flow| flow.value > 0 && flow.to != 3));
        for hub in [0, 2] {
            assert_eq!(
                flows
                    .iter()
                    .filter(|flow| flow.from == hub)
                    .map(|flow| flow.value)
                    .sum::<u64>(),
                stages[hub].value
            );
        }
    }

    #[test]
    fn empty_funnel_has_no_ribbons() {
        assert_eq!(Funnel::flows(&Funnel::default().stages()).count(), 0);
    }
}
