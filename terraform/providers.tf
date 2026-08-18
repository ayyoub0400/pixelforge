#aws config, region and default tags


provider "aws" {

	region = var.aws_region

	default_tags {
		tags = {
			Project = var.project_name
			Environment = var.environment
			ManagedBy = "terraform"
			Ephemeral = "false"
		
		}	
	}
}

#get user/account ID and ARN
data "aws_caller_identity" "current" {}
