use serde::Serialize;
use std::{
    env,
    ffi::{OsStr, OsString},
    path::{Path, PathBuf},
    process::{Command, Output},
};
use thiserror::Error;

pub const CONTAINER_NAME: &str = "woltspace";
pub const DEFAULT_IMAGE: &str = "woltspace/woltspace:latest";
const DOCKER_APP_CLI: &str = "/Applications/Docker.app/Contents/Resources/bin/docker";

#[derive(Debug, Error)]
pub enum EngineError {
    #[error("Docker is not installed or is not on PATH")]
    DockerMissing,
    #[error("could not determine the home directory")]
    HomeMissing,
    #[error("{0}")]
    Command(String),
    #[error("{0}")]
    Io(#[from] std::io::Error),
}

#[derive(Debug, Clone, Serialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct DockerDetection {
    pub installed: bool,
    pub running: bool,
    pub version: Option<String>,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct EngineStatus {
    pub state: String,
    pub image: String,
    pub container: String,
}

pub trait CommandRunner: Send + Sync {
    fn output(&self, program: &OsStr, args: &[OsString]) -> std::io::Result<Output>;
}

pub struct SystemRunner;
impl CommandRunner for SystemRunner {
    fn output(&self, program: &OsStr, args: &[OsString]) -> std::io::Result<Output> {
        Command::new(program).args(args).output()
    }
}

pub struct EngineController<R = SystemRunner> {
    runner: R,
    data_dir: PathBuf,
    image: String,
    docker_candidates: Vec<PathBuf>,
}

impl EngineController<SystemRunner> {
    pub fn from_environment() -> Result<Self, EngineError> {
        let home = env::var_os("HOME").map(PathBuf::from).ok_or(EngineError::HomeMissing)?;
        let docker_candidates = docker_candidates(&home);
        Ok(Self::with_candidates(
            SystemRunner,
            home.join(".woltspace/wolts"),
            env::var("WOLTSPACE_IMAGE").unwrap_or_else(|_| DEFAULT_IMAGE.into()),
            docker_candidates,
        ))
    }
}

impl<R: CommandRunner> EngineController<R> {
    pub fn new(runner: R, data_dir: PathBuf, image: String) -> Self {
        let candidates = docker_candidates(data_dir.parent().and_then(Path::parent).unwrap_or(Path::new("")));
        Self::with_candidates(runner, data_dir, image, candidates)
    }

    pub fn with_candidates(runner: R, data_dir: PathBuf, image: String, docker_candidates: Vec<PathBuf>) -> Self {
        Self { runner, data_dir, image, docker_candidates }
    }

    pub fn data_dir(&self) -> &Path { &self.data_dir }

    pub fn detection(&self) -> DockerDetection {
        let version = self.docker(&["--version"]);
        if let Ok(output) = version {
            let text = String::from_utf8_lossy(&output.stdout).trim().to_owned();
            let running = self.docker(&["info", "--format", "{{.ServerVersion}}"])
                .map(|result| result.status.success())
                .unwrap_or(false);
            DockerDetection { installed: true, running, version: Some(text) }
        } else {
            DockerDetection { installed: false, running: false, version: None }
        }
    }

    pub fn status(&self) -> Result<EngineStatus, EngineError> {
        let output = self.docker(&["inspect", "--format", "{{.State.Status}}", CONTAINER_NAME])?;
        let state = if output.status.success() {
            match String::from_utf8_lossy(&output.stdout).trim() {
                "running" => "running".into(),
                _ => "stopped".into(),
            }
        } else {
            "missing".into()
        };
        Ok(EngineStatus { state, image: self.image.clone(), container: CONTAINER_NAME.into() })
    }

    pub fn start(&self) -> Result<(), EngineError> { self.success(&["start", CONTAINER_NAME]).map(|_| ()) }
    pub fn pull(&self) -> Result<(), EngineError> { self.success(&["pull", &self.image]).map(|_| ()) }

    pub fn run(&self) -> Result<(), EngineError> {
        std::fs::create_dir_all(self.data_dir.join(".claude"))?;
        let mount = format!("{}:/workspace/wolts:rw", self.data_dir.display());
        let claude_mount = format!("{}:/home/node/.claude:rw", self.data_dir.join(".claude").display());
        let mut args = vec![
            "run".to_owned(), "-d".to_owned(), "--name".to_owned(), CONTAINER_NAME.to_owned(),
            "--restart".to_owned(), "unless-stopped".to_owned(),
        ];
        let env_file = self.data_dir.join(".env");
        if env_file.is_file() {
            args.extend(["--env-file".to_owned(), env_file.display().to_string()]);
        }
        args.extend([
            "-v".to_owned(), mount, "-v".to_owned(), claude_mount,
            "-p".to_owned(), "127.0.0.1:7777:7777".to_owned(),
        ]);
        #[cfg(unix)]
        args.extend([
            "-e".to_owned(), format!("HOST_UID={}", unsafe { libc::getuid() }),
            "-e".to_owned(), format!("HOST_GID={}", unsafe { libc::getgid() }),
        ]);
        args.push(self.image.clone());
        let refs = args.iter().map(String::as_str).collect::<Vec<_>>();
        self.success(&refs).map(|_| ())
    }

    pub fn logs(&self, tail: u16) -> Result<String, EngineError> {
        let tail = tail.clamp(1, 1000).to_string();
        let output = self.success(&["logs", "--tail", &tail, CONTAINER_NAME])?;
        let mut logs = String::from_utf8_lossy(&output.stdout).into_owned();
        logs.push_str(&String::from_utf8_lossy(&output.stderr));
        Ok(logs)
    }

    fn success(&self, args: &[&str]) -> Result<Output, EngineError> {
        let output = self.docker(args)?;
        if output.status.success() { Ok(output) } else {
            let message = String::from_utf8_lossy(&output.stderr).trim().to_owned();
            Err(EngineError::Command(if message.is_empty() { "Docker command failed".into() } else { message }))
        }
    }

    fn docker(&self, args: &[&str]) -> Result<Output, EngineError> {
        let args = args.iter().map(OsString::from).collect::<Vec<_>>();
        for candidate in &self.docker_candidates {
            match self.runner.output(candidate.as_os_str(), &args) {
                Ok(output) => return Ok(output),
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
                Err(error) => return Err(EngineError::Io(error)),
            }
        }
        Err(EngineError::DockerMissing)
    }
}

fn docker_candidates(home: &Path) -> Vec<PathBuf> {
    vec![
        PathBuf::from(DOCKER_APP_CLI),
        home.join(".docker/bin/docker"),
        PathBuf::from("/usr/local/bin/docker"),
        PathBuf::from("/opt/homebrew/bin/docker"),
        PathBuf::from("docker"),
    ]
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{collections::VecDeque, process::ExitStatus, sync::Mutex};

    #[cfg(unix)]
    fn status(code: i32) -> ExitStatus { use std::os::unix::process::ExitStatusExt; ExitStatus::from_raw(code << 8) }
    fn output(code: i32, stdout: &str) -> Output { Output { status: status(code), stdout: stdout.into(), stderr: vec![] } }

    enum FakeResult { Output(Output), Missing }
    struct FakeRunner { outputs: Mutex<VecDeque<FakeResult>>, calls: Mutex<Vec<(String, Vec<String>)>> }
    impl FakeRunner {
        fn new(outputs: Vec<Output>) -> Self {
            Self { outputs: Mutex::new(outputs.into_iter().map(FakeResult::Output).collect()), calls: Mutex::new(vec![]) }
        }
        fn results(outputs: Vec<FakeResult>) -> Self { Self { outputs: Mutex::new(outputs.into()), calls: Mutex::new(vec![]) } }
    }
    impl CommandRunner for FakeRunner {
        fn output(&self, program: &OsStr, args: &[OsString]) -> std::io::Result<Output> {
            self.calls.lock().unwrap().push((program.to_string_lossy().into(), args.iter().map(|v| v.to_string_lossy().into()).collect()));
            match self.outputs.lock().unwrap().pop_front().unwrap() {
                FakeResult::Output(output) => Ok(output),
                FakeResult::Missing => Err(std::io::Error::from(std::io::ErrorKind::NotFound)),
            }
        }
    }

    #[test]
    fn status_maps_an_absent_container_to_missing() {
        let engine = EngineController::with_candidates(FakeRunner::new(vec![output(1, "")]), PathBuf::from("/data"), DEFAULT_IMAGE.into(), vec!["docker".into()]);
        assert_eq!(engine.status().unwrap().state, "missing");
    }

    #[test]
    fn logs_tail_is_bounded() {
        let runner = FakeRunner::new(vec![output(0, "hello")]);
        let engine = EngineController::with_candidates(runner, PathBuf::from("/data"), DEFAULT_IMAGE.into(), vec!["docker".into()]);
        assert_eq!(engine.logs(5000).unwrap(), "hello");
        assert_eq!(engine.runner.calls.lock().unwrap()[0].1, vec!["logs", "--tail", "1000", CONTAINER_NAME]);
    }

    #[test]
    fn resolver_tries_known_locations_then_path() {
        let runner = FakeRunner::results(vec![FakeResult::Missing, FakeResult::Missing, FakeResult::Output(output(0, "Docker 27")), FakeResult::Output(output(0, "27"))]);
        let candidates = vec!["/Applications/Docker.app/Contents/Resources/bin/docker".into(), "/Users/wolt/.docker/bin/docker".into(), "docker".into()];
        let engine = EngineController::with_candidates(runner, PathBuf::from("/data"), DEFAULT_IMAGE.into(), candidates);
        assert!(engine.detection().installed);
        let calls = engine.runner.calls.lock().unwrap();
        assert_eq!(calls[0].0, DOCKER_APP_CLI);
        assert_eq!(calls[1].0, "/Users/wolt/.docker/bin/docker");
        assert_eq!(calls[2].0, "docker");
    }

    #[test]
    fn candidates_use_the_runtime_home() {
        assert_eq!(docker_candidates(Path::new("/Users/wolt"))[1], PathBuf::from("/Users/wolt/.docker/bin/docker"));
    }
}
