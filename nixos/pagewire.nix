# NixOS module for the Pagewire relay.
#
# It ships in the relay's own repository and is consumed as a flake input by the config
# repo (github.com/bcosynot/base-server), the way jhop and ouch already are:
#
#     imports = [ inputs.pagewire.nixosModules.default ];
#     services.pagewire = {
#       enable = true;
#       environmentFile = "/var/app/data/pagewire/pagewire.env";
#     };
#
# Two deliberate departures from the box's `webServer` site pattern, both from the
# design of record (section 3, "Deployment"):
#
#   * The relay is a plain systemd service running uvicorn, NOT an NGINX Unit
#     application. The periodic finalisation sweep wants a process that owns its own
#     schedule; Unit's process model does not hand one over cleanly, and a systemd timer
#     does it directly.
#   * The device-facing endpoint is a plain-HTTP nginx listener on the LAN, NOT an
#     HTTPS vhost. The device's sleep-entry drain has a ten-second deadline and cannot
#     present a trusted certificate or bring up a VPN, so TLS here would simply mean the
#     device never gets its events in.
#
# Data still follows the box's convention: /var/app/data/pagewire, systemd.tmpfiles, and
# the shared `app-writers` group.

{ self }:

{ config, lib, pkgs, ... }:

let
  cfg = config.services.pagewire;

  envFile = cfg.environmentFile;
in
{
  options.services.pagewire = {
    enable = lib.mkEnableOption "the Pagewire relay";

    package = lib.mkOption {
      type = lib.types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
      defaultText = lib.literalExpression "self.packages.\${system}.default";
      description = "The pagewire package to run, taken from this module's own flake.";
    };

    user = lib.mkOption {
      type = lib.types.str;
      default = "pagewire";
      description = "User the relay and its sweep run as.";
    };

    group = lib.mkOption {
      type = lib.types.str;
      default = "pagewire";
      description = "Group the relay and its sweep run as.";
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 8788;
      description = ''
        Loopback port uvicorn binds. Never exposed directly: the LAN-facing endpoint is
        nginx on {option}`services.pagewire.lanPort`.
      '';
    };

    lanPort = lib.mkOption {
      type = lib.types.port;
      default = 8799;
      description = ''
        Plain-HTTP LAN port the device posts its events to. This is the port that goes
        in the device's `config.json`, and the one the firewall opens. It matches the
        port the spike listener used.
      '';
    };

    stateDir = lib.mkOption {
      type = lib.types.path;
      default = "/var/app/data/pagewire";
      description = "Directory holding the SQLite file and the token/tunables file.";
    };

    environmentFile = lib.mkOption {
      type = lib.types.path;
      description = ''
        File of `PAGEWIRE_*` tunables and both service tokens, as `KEY=value` lines, read
        by the service — referenced BY PATH so that no secret is ever written into
        flake.nix. The config repo's AGENTS.md forbids new plaintext secrets there, and
        criterion 5 of the deploy ticket checks it.

        Install it out of band (mode 0600, owned by the service user). The shipped
        example is `deploy/pagewire.env.example`; the DB path in it should agree with
        {option}`services.pagewire.stateDir`.
      '';
      example = "/var/app/data/pagewire/pagewire.env";
    };

    sweepInterval = lib.mkOption {
      type = lib.types.str;
      default = "60s";
      description = ''
        How often the finalisation sweep runs. A derived sitting is only eligible for
        delivery once no new constituent event has arrived for the stitch window, so this
        is the cadence at which quiet sittings are finalised and pushed.
      '';
    };

    openFirewall = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Open {option}`lanPort` on the LAN. The device must reach it over plain HTTP.";
    };
  };

  config = lib.mkIf cfg.enable {
    # nginx carries the LAN listener. The config repo already enables it, so mkDefault
    # keeps this module usable on its own without fighting that definition.
    services.nginx.enable = lib.mkDefault true;

    users.groups.${cfg.group} = { };

    users.users.${cfg.user} = {
      isSystemUser = true;
      group = cfg.group;
      home = cfg.stateDir;
      createHome = false;
      description = "Pagewire relay";
    };

    # The box's shared group for everything that writes under /var/app/data. Lists merge
    # by concatenation, so this adds the relay without disturbing the existing members.
    users.groups.app-writers = {
      members = [ cfg.user "nginx" ];
    };

    systemd.tmpfiles.rules = [
      "d ${cfg.stateDir} 0750 ${cfg.user} ${cfg.group} -"
      "f ${cfg.stateDir}/pagewire.db 0640 ${cfg.user} ${cfg.group} -"
    ];

    # The relay itself: ingest, the identity review page, and the delivery CLI's ledger.
    systemd.services.pagewire = {
      description = "Pagewire relay — e-reader ingest and identity review";
      wantedBy = [ "multi-user.target" ];
      wants = [ "network-online.target" ];
      after = [ "network-online.target" ];

      serviceConfig = {
        ExecStart = "${cfg.package}/bin/pagewire-serve --host 127.0.0.1 --port ${toString cfg.port}";
        User = cfg.user;
        Group = cfg.group;
        WorkingDirectory = cfg.stateDir;
        EnvironmentFile = envFile;

        Restart = "on-failure";
        RestartSec = 5;

        # The relay writes only under its state directory. ProtectSystem = "strict"
        # makes the rest of the filesystem read-only, which the nix store tolerates.
        NoNewPrivileges = true;
        PrivateTmp = true;
        ProtectHome = true;
        ProtectSystem = "strict";
        ReadWritePaths = [ cfg.stateDir ];
      };
    };

    # The periodic finalisation sweep. Two commands, in order, because derivation is
    # deliberately separate from delivery: re-derivation is pure and replaces the derived
    # table wholesale, and delivery then pushes whatever has gone quiet. Type=oneshot is
    # what lets one unit carry both.
    systemd.services.pagewire-sweep = {
      description = "Pagewire finalisation sweep — derive quiet sittings, then deliver them";

      serviceConfig = {
        Type = "oneshot";
        ExecStart = [
          "${cfg.package}/bin/pagewire-derive"
          "${cfg.package}/bin/pagewire-deliver"
        ];
        User = cfg.user;
        Group = cfg.group;
        WorkingDirectory = cfg.stateDir;
        EnvironmentFile = envFile;

        NoNewPrivileges = true;
        PrivateTmp = true;
        ProtectHome = true;
        ProtectSystem = "strict";
        ReadWritePaths = [ cfg.stateDir ];
      };
    };

    systemd.timers.pagewire-sweep = {
      description = "Run the Pagewire finalisation sweep every ${cfg.sweepInterval}";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnBootSec = "1min";
        OnUnitActiveSec = cfg.sweepInterval;
        # A missed window while the box is down is not worth a catch-up run: the sweep
        # recomputes from the raw events every time, so the next run is already correct.
        Persistent = false;
      };
    };

    # The device-facing listener. Plain HTTP on the LAN, in front of the loopback app.
    # It answers on its own port rather than :80 so it cannot shadow the sites the
    # webServer module already serves there.
    services.nginx.virtualHosts."pagewire-lan" = {
      listen = [
        {
          addr = "0.0.0.0";
          port = cfg.lanPort;
        }
      ];
      forceSSL = false;
      enableACME = false;

      locations."/" = {
        proxyPass = "http://127.0.0.1:${toString cfg.port}";
        extraConfig = ''
          proxy_set_header Host $host;
          proxy_set_header X-Real-IP $remote_addr;
          # The drain gives up after ten seconds, so do not let it wait on a slow
          # upstream; a closed connection is a retry on the device, a hung one is a loss.
          proxy_read_timeout 30s;
        '';
      };
    };

    networking.firewall.allowedTCPPorts = lib.mkIf cfg.openFirewall [ cfg.lanPort ];
  };
}
