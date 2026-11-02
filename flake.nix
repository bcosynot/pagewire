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

      # `nix flake check` on its own builds the package and evaluates the devShells and
      # stops there: it does not evaluate `nixosModules`, so the module this repository
      # exists to ship would go unchecked. This imports it into a throwaway system and
      # forces the evaluation, which is what catches an option that does not exist, a
      # type that is wrong, or a missing required option.
      #
      # It evaluates rather than builds: the toplevel's derivation path is interpolated
      # into a trivial derivation, so the whole configuration is evaluated but no NixOS
      # system is built in CI.
      checks = forAllSystems (system:
        let
          pkgs = import nixpkgs { inherit system; };
          evaluated = nixpkgs.lib.nixosSystem {
            inherit system;
            modules = [
              self.nixosModules.default
              ({ pkgs, ... }: {
                # The smallest system the module can be evaluated against. Everything
                # here exists to satisfy NixOS's own required options, not to model the
                # real box — the real one is base-server's nixosConfigurations.
                services.pagewire.enable = true;
                services.pagewire.environmentFile =
                  pkgs.writeText "pagewire-check.env" "PAGEWIRE_DB=/tmp/pagewire-check.db\n";

                boot.loader.grub.enable = false;
                fileSystems."/" = {
                  device = "/dev/sda1";
                  fsType = "ext4";
                };
                system.stateVersion = "24.11";
              })
            ];
          };
        in
        {
          pagewire-module-evaluates = pkgs.runCommand "pagewire-module-evaluates" { } ''
            echo "${evaluated.config.system.build.toplevel.drvPath}" > "$out"
          '';
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
