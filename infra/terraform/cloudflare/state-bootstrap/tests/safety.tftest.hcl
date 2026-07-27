mock_provider "cloudflare" {}

run "buckets_are_distinct_and_private" {
  command = plan

  variables {
    cloudflare_account_id = "becc1c00820a0f6779e9b278ab150abf"
    state_buckets = {
      cloudflare_dns = {
        name     = "apptolast-example-dns-state"
        location = "weur"
      }
      netcup_perimeter = {
        name     = "apptolast-example-netcup-state"
        location = "weur"
      }
    }
    dockerswarm_backup_bucket = {
      name     = "apptolast-example-dockerswarm-backups"
      location = "weur"
    }
  }

  assert {
    condition = (
      length(cloudflare_r2_bucket.terraform_state) == 2 &&
      cloudflare_r2_bucket.dockerswarm_backups.name == (
        "apptolast-example-dockerswarm-backups"
      ) &&
      alltrue([
        for domain in cloudflare_r2_managed_domain.terraform_state :
        domain.enabled == false
      ]) &&
      cloudflare_r2_managed_domain.dockerswarm_backups.enabled == false
      && terraform_data.root_identity.input.root ==
      "cloudflare/state-bootstrap"
    )
    error_message = "Two state buckets and one private backup bucket are required."
  }
}

run "a_shared_bucket_is_rejected" {
  command = plan

  variables {
    cloudflare_account_id = "becc1c00820a0f6779e9b278ab150abf"
    state_buckets = {
      cloudflare_dns = {
        name     = "apptolast-example-shared-state"
        location = "weur"
      }
      netcup_perimeter = {
        name     = "apptolast-example-shared-state"
        location = "weur"
      }
    }
    dockerswarm_backup_bucket = {
      name     = "apptolast-example-dockerswarm-backups"
      location = "weur"
    }
  }

  expect_failures = [
    var.state_buckets,
  ]
}

run "a_state_bucket_cannot_be_reused_for_backups" {
  command = plan

  variables {
    cloudflare_account_id = "becc1c00820a0f6779e9b278ab150abf"
    state_buckets = {
      cloudflare_dns = {
        name     = "apptolast-example-dns-state"
        location = "weur"
      }
      netcup_perimeter = {
        name     = "apptolast-example-netcup-state"
        location = "weur"
      }
    }
    dockerswarm_backup_bucket = {
      name     = "apptolast-example-dns-state"
      location = "weur"
    }
  }

  expect_failures = [
    var.dockerswarm_backup_bucket,
  ]
}
