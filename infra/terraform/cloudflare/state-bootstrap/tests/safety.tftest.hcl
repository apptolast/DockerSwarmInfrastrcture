mock_provider "cloudflare" {}

run "buckets_are_distinct_and_private" {
  command = plan

  variables {
    cloudflare_account_id = "00000000000000000000000000000000"
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
  }

  assert {
    condition = (
      length(cloudflare_r2_bucket.terraform_state) == 2 &&
      alltrue([
        for domain in cloudflare_r2_managed_domain.terraform_state :
        domain.enabled == false
      ])
    )
    error_message = "Exactly two private state buckets must be declared."
  }
}

run "a_shared_bucket_is_rejected" {
  command = plan

  variables {
    cloudflare_account_id = "00000000000000000000000000000000"
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
  }

  expect_failures = [
    var.state_buckets,
  ]
}
