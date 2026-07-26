output "state_buckets" {
  description = "Bucket names and locations used by the production roots."
  value = {
    for key, bucket in cloudflare_r2_bucket.terraform_state :
    key => {
      name     = bucket.name
      location = bucket.location
    }
  }
}
