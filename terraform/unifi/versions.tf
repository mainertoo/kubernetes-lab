terraform {
  required_version = ">= 1.5"

  required_providers {
    unifi = {
      source  = "filipowm/unifi"
      version = "~> 1.0"
    }
  }
}
