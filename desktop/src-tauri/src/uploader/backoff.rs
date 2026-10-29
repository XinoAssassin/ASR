//! Exponential retry backoff with jitter.

use std::time::Duration;

pub const BASE: Duration = Duration::from_secs(10);
pub const CAP: Duration = Duration::from_secs(30 * 60);

/// Delay before retry number `failures` (1 = first failure). `jitter` in [-1, 1] scales +-20 %.
pub fn delay(failures: u32, jitter: f64) -> Duration {
    let exp = failures.saturating_sub(1).min(20);
    let base = BASE.as_secs_f64() * 2f64.powi(exp as i32);
    let d = base.min(CAP.as_secs_f64()) * (1.0 + 0.2 * jitter.clamp(-1.0, 1.0));
    Duration::from_secs_f64(d)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn grows_and_caps() {
        assert_eq!(delay(1, 0.0), Duration::from_secs(10));
        assert_eq!(delay(2, 0.0), Duration::from_secs(20));
        assert_eq!(delay(4, 0.0), Duration::from_secs(80));
        assert_eq!(delay(30, 0.0), CAP);
        assert_eq!(delay(1, 1.0), Duration::from_secs(12));
        assert_eq!(delay(1, -5.0), Duration::from_secs(8));
        assert!(delay(0, 0.0) == Duration::from_secs(10));
    }
}
