{
  description = "Pagewire — relay reading sessions from a Crosspoint e-reader to Readwise Books and Hardcover";

  inputs = {
    # Unpinned on purpose, matching the rest of the box's config: the relay is pure
    # Python with no native build, so it does not need the revision lockstep that the
    # poetry2nix apps (jhop, ouch) do.
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      # The relay runs on the NixOS LXC (x86_64) and is developed on an aarch64 box.
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems f;

      pagewireFor = system:
        let
          pkgs = import nixpkgs { inherit system; };
        in
        pkgs.python3Packages.buildPythonApplication {
          pname = "pagewire";
          version = "0.0.1";
          pyproject = true;
          src = ./.;
          build-system = [ pkgs.python3Packages.setuptools ];
          dependencies = with pkgs.python3Packages; [
            fastapi
            uvicorn
          ];
          # The suite writes SQLite files and expects a writable workspace; it is run
          # with `make test` in a checkout, not inside the sandbox. What this derivation
          # exists to produce is the console scripts the systemd units start.
          doCheck = false;
        };
    in
    {
      packages = forAllSystems (system: {
        default = pagewireFor system;
        pagewire = pagewireFor system;
      });

      devShells = forAllSystems (system:
        let
          pkgs = import nixpkgs { inherit system; };
        in
        {
          default = pkgs.mkShell {
            packages = [
              (pkgs.python3.withPackages (ps: with ps; [
                fastapi
                uvicorn
                pytest
                httpx
              ]))
            ];
          };
        });

      # The NixOS module ships here rather than in the config repo, so the config repo
      # consumes the relay exactly the way it already consumes jhop and ouch — as a
      # flake input by URL:
      #
      #     imports = [ inputs.pagewire.nixosModules.default ];
      #
      # Nothing in the module is app source; it is the service, the listener, the
      # state directory and the firewall port.
      nixosModules.default = import ./nixos/pagewire.nix { inherit self; };
      nixosModules.pagewire = self.nixosModules.default;
    };
}
