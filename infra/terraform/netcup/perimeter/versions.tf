terraform {
  required_version = "= 1.15.8"

  backend "s3" {}

  required_providers {
    netcup = {
      source  = "hornc-greedy/netcup"
      version = "= 1.0.0"
    }
  }
}
