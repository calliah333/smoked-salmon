{
  description = "smoked-salmon CLI and development environment";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
    };

    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      pyproject-nix,
      uv2nix,
      pyproject-build-systems,
      ...
    }:
    let
      inherit (nixpkgs) lib;
      forAllSystems = lib.genAttrs lib.systems.flakeExposed;

      workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };

      pythonSets = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          baseSet = pkgs.callPackage pyproject-nix.build.packages {
            python = pkgs.python313;
          };
        in
        baseSet.overrideScope (
          lib.composeManyExtensions [
            pyproject-build-systems.overlays.wheel
            (workspace.mkPyprojectOverlay { sourcePreference = "wheel"; })
          ]
        )
      );
    in
    {
      packages = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          pythonSet = pythonSets.${system};
          inherit (pkgs.callPackages pyproject-nix.build.util { }) mkApplication;
          runtimeDependencies = with pkgs; [
            curl
            flac
            git
            lame
            mp3val
            rclone
            sox
          ];
          salmonApplication = mkApplication {
            venv = pythonSet.mkVirtualEnv "smoked-salmon-env" workspace.deps.default;
            package = pythonSet.salmon;
          };
          salmon = pkgs.stdenvNoCC.mkDerivation {
            pname = "smoked-salmon";
            version = pythonSet.salmon.version;
            dontUnpack = true;
            nativeBuildInputs = [ pkgs.makeWrapper ];
            installPhase = ''
              runHook preInstall
              mkdir -p $out/bin
              makeWrapper ${salmonApplication}/bin/salmon $out/bin/salmon \
                --prefix PATH : ${lib.makeBinPath runtimeDependencies}
              runHook postInstall
            '';
            meta = {
              description = "Uploading script for Gazelle-based music trackers";
              homepage = "https://github.com/smokin-salmon/smoked-salmon";
              license = lib.licenses.asl20;
              mainProgram = "salmon";
            };
          };
        in
        {
          default = salmon;
          inherit salmon;
        }
      );

      apps = forAllSystems (system: {
        default = {
          type = "app";
          program = "${lib.getExe self.packages.${system}.default}";
          meta.description = "Run the smoked-salmon CLI";
        };
      });

      devShells = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          pythonSet = pythonSets.${system}.overrideScope (
            workspace.mkEditablePyprojectOverlay { root = "$REPO_ROOT"; }
          );
          virtualenv = pythonSet.mkVirtualEnv "smoked-salmon-dev-env" workspace.deps.all;
        in
        {
          default = pkgs.mkShell {
            packages = [
              virtualenv
              pkgs.uv
              pkgs.curl
              pkgs.flac
              pkgs.git
              pkgs.lame
              pkgs.mp3val
              pkgs.rclone
              pkgs.sox
            ];
            env = {
              UV_NO_SYNC = "1";
              UV_PYTHON = pythonSet.python.interpreter;
              UV_PYTHON_DOWNLOADS = "never";
            };
            shellHook = ''
              unset PYTHONPATH
              export REPO_ROOT="$(git rev-parse --show-toplevel)"
            '';
          };
        }
      );
    };
}
