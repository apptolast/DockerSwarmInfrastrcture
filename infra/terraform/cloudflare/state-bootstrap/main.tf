resource "cloudflare_r2_bucket" "terraform_state" {
  for_each = var.state_buckets

  account_id    = var.cloudflare_account_id
  name          = each.value.name
  location      = each.value.location
  storage_class = "Standard"

  lifecycle {
    prevent_destroy = true
  }
}

# R2 buckets are private by default. Managing the provider-owned r2.dev switch
# as disabled makes that part of the reviewed contract instead of a UI choice.
resource "cloudflare_r2_managed_domain" "terraform_state" {
  for_each = var.state_buckets

  account_id  = var.cloudflare_account_id
  bucket_name = cloudflare_r2_bucket.terraform_state[each.key].name
  enabled     = false
}
