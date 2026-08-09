terraform {
  required_version = "= 1.15.8"

  # This root creates the remote-state buckets, so its own state cannot start
  # inside either bucket. Supply an encrypted off-repository path at init time.
  backend "local" {}

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "5.23.0"
    }
  }
}
