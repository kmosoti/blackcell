"""Single operation catalog for SDK and CLI facade contracts."""

from enum import StrEnum

from blackcell.contracts.facade import (
    Authority,
    Credential,
    Effect,
    Facade,
    InvariantAspect,
    InvariantGroup,
    OperationSpec,
    operation,
)


class OperationId(StrEnum):
    PROFILE_VALIDATE = "profile.validate"
    PROFILE_SHOW = "profile.show"
    SCHEMA_AUDIT = "schema.audit"
    DIRECTIVE_VALIDATE = "directive.validate"
    DIRECTIVE_PROPOSE = "directive.propose"
    DIRECTIVE_STATUS = "directive.status"
    DIRECTIVE_MATERIALIZE = "directive.materialize"
    DIRECTIVE_RECONCILE = "directive.reconcile"
    OPERATION_INSPECT = "operation.inspect"
    OPERATION_RECONCILE = "operation.reconcile"
    OPERATION_VERIFY = "operation.verify"
    ASSIGNMENT_LIST = "assignment.list"
    ASSIGNMENT_VERIFY = "assignment.verify"
    ECHO_VERIFY = "echo.verify"
    RECON_STATUS = "recon.status"
    WORKFLOW_RUN = "workflow.run"
    WORKFLOW_STATUS = "workflow.status"
    WORKFLOW_RESUME = "workflow.resume"
    CHRONICLE_SHOW = "chronicle.show"
    ANOMALY_LIST = "anomaly.list"
    ANOMALY_RESOLVE = "anomaly.resolve"
    PULSE = "system.pulse"
    PUBLICATION_PREFLIGHT = "publication.preflight"


def _specs() -> dict[OperationId, OperationSpec]:
    input_authority = (InvariantAspect.INPUT, InvariantAspect.AUTHORITY)
    remote_identity = (
        InvariantAspect.AUTHENTICATION,
        InvariantAspect.IDENTITY,
        InvariantAspect.AUTHORITY,
    )
    immutable_remote = (
        *remote_identity,
        InvariantAspect.STATE,
        InvariantAspect.IMMUTABILITY,
    )
    schema_digest = (InvariantGroup.SCHEMA, InvariantGroup.DIGEST)
    project_contract = (
        InvariantGroup.AUTHORITY,
        InvariantGroup.CREDENTIAL,
        InvariantGroup.IDENTITY,
        InvariantGroup.LIFECYCLE,
        InvariantGroup.DIGEST,
        InvariantGroup.PROJECT_WORKFLOW,
        InvariantGroup.PROJECT_PRESENTATION,
    )
    assignment_contract = (
        InvariantGroup.AUTHORITY,
        InvariantGroup.CREDENTIAL,
        InvariantGroup.IDENTITY,
        InvariantGroup.LIFECYCLE,
        InvariantGroup.DIGEST,
        InvariantGroup.ASSIGNMENT_CONTRACT,
    )
    echo_contract = (
        InvariantGroup.AUTHORITY,
        InvariantGroup.CREDENTIAL,
        InvariantGroup.ECHO_CONTRACT,
    )
    return {
        OperationId.PROFILE_VALIDATE: operation(
            OperationId.PROFILE_VALIDATE,
            Facade.PROFILE,
            Authority.BLACKCELL,
            Effect.READ,
            InvariantAspect.INPUT,
            invariant_groups=(InvariantGroup.SCHEMA, InvariantGroup.AUTHORITY),
        ),
        OperationId.PROFILE_SHOW: operation(
            OperationId.PROFILE_SHOW,
            Facade.PROFILE,
            Authority.BLACKCELL,
            Effect.READ,
            InvariantAspect.INPUT,
            invariant_groups=(InvariantGroup.SCHEMA, InvariantGroup.AUTHORITY),
        ),
        OperationId.SCHEMA_AUDIT: operation(
            OperationId.SCHEMA_AUDIT,
            Facade.SCHEMA,
            Authority.BLACKCELL,
            Effect.READ,
            InvariantAspect.INPUT,
            InvariantAspect.IMMUTABILITY,
            invariant_groups=schema_digest,
        ),
        OperationId.DIRECTIVE_VALIDATE: operation(
            OperationId.DIRECTIVE_VALIDATE,
            Facade.DIRECTIVE,
            Authority.BLACKCELL,
            Effect.READ,
            *input_authority,
            InvariantAspect.IMMUTABILITY,
            invariant_groups=(
                InvariantGroup.SCHEMA,
                InvariantGroup.AUTHORITY,
                InvariantGroup.DIGEST,
            ),
        ),
        OperationId.DIRECTIVE_PROPOSE: operation(
            OperationId.DIRECTIVE_PROPOSE,
            Facade.DIRECTIVE,
            Authority.LINEAR,
            Effect.RECONCILE,
            *input_authority,
            InvariantAspect.AUTHENTICATION,
            InvariantAspect.IDENTITY,
            InvariantAspect.STATE,
            InvariantAspect.IMMUTABILITY,
            InvariantAspect.IDEMPOTENCY,
            credentials=(Credential.LINEAR,),
            invariant_groups=project_contract,
        ),
        OperationId.DIRECTIVE_STATUS: operation(
            OperationId.DIRECTIVE_STATUS,
            Facade.DIRECTIVE,
            Authority.LINEAR,
            Effect.READ,
            *immutable_remote,
            credentials=(Credential.LINEAR,),
            invariant_groups=project_contract,
        ),
        OperationId.DIRECTIVE_MATERIALIZE: operation(
            OperationId.DIRECTIVE_MATERIALIZE,
            Facade.DIRECTIVE,
            Authority.LINEAR,
            Effect.MUTATE,
            *immutable_remote,
            InvariantAspect.IDEMPOTENCY,
            credentials=(Credential.LINEAR, Credential.GITHUB),
            invariant_groups=(
                *assignment_contract,
                InvariantGroup.ECHO_CONTRACT,
            ),
        ),
        OperationId.DIRECTIVE_RECONCILE: operation(
            OperationId.DIRECTIVE_RECONCILE,
            Facade.DIRECTIVE,
            Authority.CROSS_SYSTEM,
            Effect.RECONCILE,
            *immutable_remote,
            InvariantAspect.IDEMPOTENCY,
            credentials=(Credential.LINEAR, Credential.GITHUB),
            invariant_groups=(
                *assignment_contract,
                InvariantGroup.ECHO_CONTRACT,
            ),
        ),
        OperationId.OPERATION_INSPECT: operation(
            OperationId.OPERATION_INSPECT,
            Facade.OPERATION,
            Authority.LINEAR,
            Effect.READ,
            *remote_identity,
            InvariantAspect.IMMUTABILITY,
            credentials=(Credential.LINEAR,),
            invariant_groups=project_contract,
        ),
        OperationId.OPERATION_RECONCILE: operation(
            OperationId.OPERATION_RECONCILE,
            Facade.OPERATION,
            Authority.LINEAR,
            Effect.RECONCILE,
            *immutable_remote,
            InvariantAspect.IDEMPOTENCY,
            credentials=(Credential.LINEAR,),
            invariant_groups=project_contract,
        ),
        OperationId.OPERATION_VERIFY: operation(
            OperationId.OPERATION_VERIFY,
            Facade.OPERATION,
            Authority.LINEAR,
            Effect.READ,
            *immutable_remote,
            credentials=(Credential.LINEAR,),
            invariant_groups=project_contract,
        ),
        OperationId.ASSIGNMENT_LIST: operation(
            OperationId.ASSIGNMENT_LIST,
            Facade.ASSIGNMENT,
            Authority.LINEAR,
            Effect.READ,
            *remote_identity,
            credentials=(Credential.LINEAR,),
            invariant_groups=(
                InvariantGroup.AUTHORITY,
                InvariantGroup.CREDENTIAL,
                InvariantGroup.IDENTITY,
                InvariantGroup.ASSIGNMENT_CONTRACT,
            ),
        ),
        OperationId.ASSIGNMENT_VERIFY: operation(
            OperationId.ASSIGNMENT_VERIFY,
            Facade.ASSIGNMENT,
            Authority.LINEAR,
            Effect.READ,
            *immutable_remote,
            credentials=(Credential.LINEAR,),
            invariant_groups=assignment_contract,
        ),
        OperationId.ECHO_VERIFY: operation(
            OperationId.ECHO_VERIFY,
            Facade.ECHO,
            Authority.GITHUB,
            Effect.READ,
            InvariantAspect.AUTHENTICATION,
            InvariantAspect.AUTHORITY,
            InvariantAspect.IMMUTABILITY,
            credentials=(Credential.GITHUB,),
            invariant_groups=echo_contract,
        ),
        OperationId.RECON_STATUS: operation(
            OperationId.RECON_STATUS,
            Facade.RECON,
            Authority.CROSS_SYSTEM,
            Effect.READ,
            *immutable_remote,
            credentials=(Credential.LINEAR, Credential.GITHUB),
            invariant_groups=(
                *project_contract,
                InvariantGroup.ASSIGNMENT_CONTRACT,
                InvariantGroup.ECHO_CONTRACT,
            ),
        ),
        OperationId.WORKFLOW_RUN: operation(
            OperationId.WORKFLOW_RUN,
            Facade.WORKFLOW,
            Authority.CROSS_SYSTEM,
            Effect.RECONCILE,
            *immutable_remote,
            InvariantAspect.IDEMPOTENCY,
            credentials=(Credential.LINEAR,),
            invariant_groups=(
                InvariantGroup.SCHEMA,
                *project_contract,
                InvariantGroup.ASSIGNMENT_CONTRACT,
                InvariantGroup.ECHO_CONTRACT,
                InvariantGroup.PUBLICATION_IDENTITY,
            ),
        ),
        OperationId.WORKFLOW_STATUS: operation(
            OperationId.WORKFLOW_STATUS,
            Facade.WORKFLOW,
            Authority.BLACKCELL,
            Effect.READ,
            InvariantAspect.IMMUTABILITY,
            invariant_groups=(
                InvariantGroup.SCHEMA,
                InvariantGroup.DIGEST,
                InvariantGroup.LIFECYCLE,
            ),
        ),
        OperationId.WORKFLOW_RESUME: operation(
            OperationId.WORKFLOW_RESUME,
            Facade.WORKFLOW,
            Authority.CROSS_SYSTEM,
            Effect.RECONCILE,
            *immutable_remote,
            InvariantAspect.IDEMPOTENCY,
            credentials=(Credential.LINEAR,),
            invariant_groups=(
                InvariantGroup.SCHEMA,
                *project_contract,
                InvariantGroup.ASSIGNMENT_CONTRACT,
                InvariantGroup.ECHO_CONTRACT,
                InvariantGroup.PUBLICATION_IDENTITY,
            ),
        ),
        OperationId.CHRONICLE_SHOW: operation(
            OperationId.CHRONICLE_SHOW,
            Facade.CHRONICLE,
            Authority.BLACKCELL,
            Effect.READ,
            InvariantAspect.IMMUTABILITY,
            invariant_groups=(InvariantGroup.DIGEST,),
        ),
        OperationId.ANOMALY_LIST: operation(
            OperationId.ANOMALY_LIST,
            Facade.ANOMALY,
            Authority.BLACKCELL,
            Effect.READ,
            InvariantAspect.IMMUTABILITY,
            invariant_groups=(InvariantGroup.DIGEST,),
        ),
        OperationId.ANOMALY_RESOLVE: operation(
            OperationId.ANOMALY_RESOLVE,
            Facade.ANOMALY,
            Authority.BLACKCELL,
            Effect.APPEND,
            InvariantAspect.INPUT,
            InvariantAspect.IMMUTABILITY,
            invariant_groups=(InvariantGroup.SCHEMA, InvariantGroup.DIGEST),
        ),
        OperationId.PULSE: operation(
            OperationId.PULSE,
            Facade.SYSTEM,
            Authority.CROSS_SYSTEM,
            Effect.READ,
            *remote_identity,
            credentials=(Credential.LINEAR, Credential.GITHUB),
            invariant_groups=(
                InvariantGroup.AUTHORITY,
                InvariantGroup.CREDENTIAL,
                InvariantGroup.IDENTITY,
                InvariantGroup.LIFECYCLE,
            ),
        ),
        OperationId.PUBLICATION_PREFLIGHT: operation(
            OperationId.PUBLICATION_PREFLIGHT,
            Facade.PUBLICATION,
            Authority.LOCAL_GIT,
            Effect.READ,
            InvariantAspect.IDENTITY,
            InvariantAspect.AUTHORITY,
            InvariantAspect.STATE,
            InvariantAspect.PUBLICATION_IDENTITY,
            invariant_groups=(
                InvariantGroup.AUTHORITY,
                InvariantGroup.LIFECYCLE,
                InvariantGroup.PUBLICATION_IDENTITY,
            ),
        ),
    }


OPERATIONS = _specs()


def spec(operation_id: OperationId) -> OperationSpec:
    return OPERATIONS[operation_id]
