import os
import base64
import datetime
import hashlib
from collections import namedtuple

from django.core.exceptions import ObjectDoesNotExist
from django.http import Http404
from rest_framework.exceptions import ValidationError, PermissionDenied

from panoramix import models
from panoramix import canonical
from panoramix import utils
from panoramix.skeleton import CreateView, PartialUpdateView
from panoramix.config import get_server_backend

backend = get_server_backend()

get_now = datetime.datetime.utcnow


def get_instance(model, filters, for_update=False):
    objs = model.objects
    if for_update:
        objs = objs.select_for_update()
    try:
        return objs.get(**filters)
    except ObjectDoesNotExist:
        raise Http404


def generate_random_key():
    s = os.urandom(32)
    return base64.urlsafe_b64encode(s).rstrip('=')


def contribute_to_negotiation(negotiation, request=None, key_data=None):
    if negotiation.status != models.NegotiationStatus.OPEN:
        raise ValidationError("neg is not open")
    text = request["text"]
    signature = request["signature"]
    signer_key_id = request["signer_key_id"]
    valid, key_id = verify(text, signature, public=key_data)
    if not valid:
        raise PermissionDenied("contribution's signature is not valid")
    if signer_key_id != key_id:
        raise PermissionDenied("signer_key_id mismatch")

    negotiation.contributions.filter(signer_key_id=key_id).update(latest=False)
    contrib = models.Contribution.objects.create(
        latest=True,
        negotiation=negotiation, text=text,
        signer_key_id=key_id, signature=signature)
    check_close_negotiation(negotiation)
    return contrib


def check_close_negotiation(negotiation):
    latests = negotiation.get_latest_contributions()
    text_set = set(c.text for c in latests)
    if len(text_set) == 1:
        text = text_set.pop()
        unpacked_text = canonical.from_canonical(utils.from_unicode(text))
        meta = unpacked_text.get("meta", {})
        accept = meta.get("accept", False)
        if accept:
            _close_negotiation(negotiation, latests)


def get_text(contributions):
    texts = set(c.text for c in contributions)
    assert len(texts) == 1
    return texts.pop()


def mk_signings(negotiation, contributions):
    signings_dict = {}
    signings = []
    for contribution in contributions:
        signings.append(models.Signing(
            negotiation=negotiation,
            signer_key_id=contribution.signer_key_id,
            signature=contribution.signature))
        signings_dict[contribution.signer_key_id] = contribution.signature
    models.Signing.objects.bulk_create(signings)
    return signings_dict


def _close_negotiation(negotiation, contributions):
    now = get_now()
    negotiation.text = get_text(contributions)
    signings_dict = mk_signings(negotiation, contributions)
    hashable = {
        "timestamp": now.isoformat(),
        "negotiation_id": negotiation.id,
        "text": negotiation.text,
        "signings": signings_dict,
    }
    consensus = utils.hash_string(canonical.to_canonical(hashable))
    negotiation.timestamp = now
    negotiation.consensus = consensus
    negotiation.status = models.NegotiationStatus.DONE
    negotiation.save()


def retrieve_consensus(consensus_id=None):
    consensus = get_instance(models.Negotiation, {'consensus': consensus_id})
    return consensus.to_consensus_dict()


def assert_consensus_signed(owners, signings):
    if not signings:
        raise PermissionDenied("No signings found")
    for owner in owners:
        signature = signings.get(owner)
        if signature is None:
            raise PermissionDenied("%s has not signed" % owner)


def check_permission(peer_id, owners, signings, request_peer_id):
    if owners:
        if request_peer_id not in owners:
            raise PermissionDenied(
                "request user %s is not a peer owner" % request_peer_id)
        expected_signers = owners
    else:
        if request_peer_id != peer_id:
            raise PermissionDenied(
                "request user %s cannot operate on peer %s" %
                (request_peer_id, peer_id))

        expected_signers = [request_peer_id]
    assert_consensus_signed(expected_signers, signings)


def get_body_and_meta(consensus):
    consensus_text = consensus["text"]
    unpacked_text = canonical.from_canonical(
        utils.from_unicode(consensus_text))
    body = unpacked_text.get("body", {})
    canonical_body = canonical.to_canonical(body)
    meta = unpacked_text.get("meta", {})
    return canonical_body, meta


def check_all_signed(signings, meta):
    signers = meta.get("signers", [])
    actual_signers = signings.keys()
    if signers and sorted(signers) != sorted(actual_signers):
        raise ValidationError("no proper set of signatures")


def check_bodies_equal(expected_body, consensus_body):
    if expected_body != consensus_body:
        raise ValidationError("different consensus texts")


def check_accepted(meta):
    accept = meta.get("accept", False)
    if not accept:
        raise ValidationError("not an accepted text")


def handle_consensus(expected_body, consensus_id):
    consensus = retrieve_consensus(consensus_id)
    signings = consensus["signings"]
    body, meta = get_body_and_meta(consensus)

    check_bodies_equal(expected_body, body)
    check_accepted(meta)
    check_all_signed(signings, meta)
    return signings


def validate_operation(info, operation, resource):
    actual_operation = info.get("operation")
    actual_resource = info.get("resource")
    if operation != actual_operation:
        raise ValidationError("Operation is not %s." % operation)
    if resource != actual_resource:
        raise ValidationError("Resource is not %s." % resource)


def get_requested_consensus_body(request_data):
    structure = {
        "data": request_data.get("data"),
        "info": request_data.get("info"),
    }
    return canonical.to_canonical(structure)


T_STRUCTURAL = "structural"


def require_by_consensus(request):
    request_data = request.data
    by_consensus = request_data.get("by_consensus", {})
    if not by_consensus:
        raise PermissionDenied(
            "Creating an endpoint without consensus is not allowed.")

    consensus_id = by_consensus.get("consensus_id", None)
    if consensus_id is None:
        raise ValidationError("consensus is missing")

    consensus_type = by_consensus.get("consensus_type")
    if consensus_type == T_STRUCTURAL:
        consensus_body = get_requested_consensus_body(request_data)
    return consensus_id, consensus_body


class PeerView(CreateView):
    resource_name = "peer"

    def creation_logic(self, request):
        request_user = request.user.peer_id
        request_data = request.data

        data = request_data.get("data", {})
        info = request_data.get("info", {})
        validate_operation(info, "create", self.resource_name)
        consensus_id, consensus_body = require_by_consensus(request)
        signings = handle_consensus(consensus_body, consensus_id)

        owners = data.get("owners", [])
        owners = [owner["owner_key_id"] for owner in owners]
        peer_id = data["peer_id"]
        status = data.get("status")
        if status != models.PeerStatus.READY:
            raise ValidationError("unexpected status")

        check_permission(peer_id, owners, signings, request_user)
        peer = self.perform_create(data, consensus_id)
        return peer

    def perform_create(self, data, consensus_id=None):
        owners = data.pop("owners")
        owners = [owner["owner_key_id"] for owner in owners]

        peer = models.Peer.objects.create(**data)
        owner_entries = [models.Owner(peer_id=peer.peer_id, owner_key_id=owner)
                         for owner in owners]
        models.Owner.objects.bulk_create(owner_entries)
        peer.log_consensus(consensus_id)
        backend.register_key(data["key_data"])
        return peer


class ContributionView(CreateView):
    resource_name = "contribution"

    def creation_logic(self, request):
        key_data = request.auth
        request_data = request.data
        data = request_data.get("data", {})
        info = request_data.get("info", {})
        validate_operation(info, "create", self.resource_name)
        negotiation_href = data.get("negotiation").rstrip('/')
        negotiation_id = negotiation_href.split('/')[-1]
        negotiation = get_instance(
            models.Negotiation, {'pk': negotiation_id}, for_update=True)
        contrib = contribute_to_negotiation(
            negotiation, data, key_data=key_data)
        return contrib

    def retrieve(self, request, *args, **kwargs):
        if not self.request.query_params.get("negotiation"):
            raise PermissionDenied("must filter by negotiation")
        return super(ContributionView, self).retrieve(request, *args, **kwargs)

    def list(self, request, *args, **kwargs):
        if not self.request.query_params.get("negotiation"):
            raise PermissionDenied("must filter by negotiation")

        # request_data = self.request.data
        # data = request_data.get("data", {})
        # info = request_data.get("info", {})
        # validate_operation(info, self.action, self.resource_name)
        # negotiation_id = data.get("negotiation")

        return super(ContributionView, self).list(request, *args, **kwargs)


class NegotiationView(CreateView):
    resource_name = "negotiation"

    def new_negotiation(self):
        return models.Negotiation.objects.create(id=generate_random_key())

    def creation_logic(self, request):
        request_data = request.data
        data = request_data.get("data", {})
        info = request_data.get("info", {})
        validate_operation(info, "create", self.resource_name)
        return self.new_negotiation()

    def list(self, request, *args, **kwargs):
        if not self.request.query_params.get("consensus"):
            raise PermissionDenied("must filter by consensus")
        return super(NegotiationView, self).list(request, *args, **kwargs)


class EndpointView(CreateView, PartialUpdateView):
    resource_name = "endpoint"

    def creation_logic(self, request):
        request_user = request.user.peer_id
        request_data = request.data
        data = request_data.get("data", {})
        info = request_data.get("info", {})
        validate_operation(info, "create", self.resource_name)
        consensus_id, consensus_body = require_by_consensus(request)
        signings = handle_consensus(consensus_body, consensus_id)

        peer_id = data["peer_id"]
        peer = get_instance(models.Peer, {'peer_id': peer_id}, for_update=True)
        if peer.status != models.PeerStatus.READY:
            raise PermissionDenied("peer is not in state READY")
        owners = peer.list_owners()
        check_permission(peer_id, owners, signings, request_user)
        endpoint = self.perform_create(data, consensus_id)
        return endpoint

    def perform_create(self, data, consensus_id):
        status = data.get("status")
        if status != models.CycleStatus.OPEN:
            raise ValidationError("unexpected status")

        endpoint = models.Endpoint.objects.create(**data)
        endpoint.log_consensus(consensus_id)
        return endpoint

    def partial_update_logic(self, request, endpoint):
        request_user = request.user.peer_id
        request_data = request.data
        data = request_data.get("data", {})
        info = request_data.get("info", {})
        validate_operation(info, "partial_update", self.resource_name)
        consensus_id, consensus_body = require_by_consensus(request)

        on_last_consensus_id = info.get("on_last_consensus_id")
        if on_last_consensus_id is None:
            raise PermissionDenied(
                "cannot update endpoint without reference "
                "to last consensus id")

        endpoint_id = info["id"]
        if endpoint.endpoint_id != endpoint_id:
            raise PermissionDenied("endpoint_id mismatch")

        last_consensus_id = endpoint.get_last_consensus_id()
        if last_consensus_id != on_last_consensus_id:
            raise PermissionDenied(
                "condition on consensus_id failed")

        signings = handle_consensus(consensus_body, consensus_id)
        peer_id = endpoint.peer_id
        peer = get_instance(models.Peer, {'peer_id': peer_id})
        if peer.status != models.PeerStatus.READY:
            raise PermissionDenied("peer is not in state READY")
        owners = peer.list_owners()
        check_permission(peer_id, owners, signings, request_user)
        apply_transition(endpoint, data, consensus_id)
        return endpoint


def apply_transition(endpoint, data, consensus_id):
    requested_status = data.get("status")
    if requested_status == models.CycleStatus.CLOSED:
        close_endpoint(endpoint, data)
    elif requested_status == models.CycleStatus.PROCESSED:
        record_endpoint_process(endpoint, data)
    else:
        raise ValidationError("invalid status")
    endpoint.log_consensus(consensus_id)


def compute_messages_hash(msg_hashes):
    sorted_hashes = sorted(set(msg_hashes))
    hasher = hashlib.sha256()
    for msg_hash in sorted_hashes:
        hasher.update(msg_hash)
    return hasher.hexdigest()


def close_endpoint(endpoint, data):
    current_status = endpoint.status
    if current_status not in [
            models.CycleStatus.OPEN, models.CycleStatus.FULL]:
        raise PermissionDenied("wrong current state")

    message_hashes = data.get("message_hashes", [])
    message_hashes = [d["hash"] for d in message_hashes]
    message_hashes_count = len(message_hashes)
    selected_inbox_messages = models.Message.objects.filter(
        message_hash__in=message_hashes,
        endpoint_id=endpoint.endpoint_id,
        box=models.Box.INBOX)

    inbox_count = selected_inbox_messages.count()
    if message_hashes_count != inbox_count:
        raise PermissionDenied("message count mismatch")
    if inbox_count < endpoint.size_min:
        raise PermissionDenied("Didn't reach min size")
    if inbox_count > endpoint.size_max:
        raise PermissionDenied("Too many messages")

    inbox_hash = compute_messages_hash(message_hashes)
    endpoint.inbox_hash = inbox_hash
    endpoint.status = models.CycleStatus.CLOSED
    endpoint.save()
    selected_inbox_messages.update(box=models.Box.ACCEPTED)


def record_endpoint_process(endpoint, data):
    process_proof = data.get("process_proof")
    message_hashes = data.get("message_hashes", [])
    message_hashes = [d["hash"] for d in message_hashes]
    message_hashes_count = len(message_hashes)
    processed_messages = models.Message.objects.filter(
        message_hash__in=message_hashes,
        endpoint_id=endpoint.endpoint_id,
        box=models.Box.PROCESSBOX)
    count = processed_messages.count()
    if message_hashes_count != count:
        raise PermissionDenied("message count mismatch")

    outbox_hash = compute_messages_hash(message_hashes)
    endpoint.process_proof = process_proof
    endpoint.outbox_hash = outbox_hash
    endpoint.status = models.CycleStatus.PROCESSED
    endpoint.save()
    processed_messages.update(box=models.Box.OUTBOX)


def check_is_owner(endpoint, request_user_id):
    peer_id = endpoint.peer_id
    if peer_id == request_user_id:
        return True
    return models.Owner.objects.filter(
        peer_id=peer_id, owner_key_id=request_user_id).exists()


class MessageView(CreateView):
    resource_name = "message"

    def creation_logic(self, request):
        request_data = request.data
        request_user = request.user.peer_id
        data = request_data.get("data", {})
        info = request_data.get("info", {})
        validate_operation(info, "create", self.resource_name)
        endpoint_id = data.get("endpoint_id")
        box = data.get("box")
        endpoint = get_instance(
            models.Endpoint, {'endpoint_id': endpoint_id}, for_update=True)

        if box == models.Box.INBOX:
            check_cycle_is_open(endpoint)

        elif box == models.Box.PROCESSBOX:
            check_cycle_can_process(endpoint)
            check_is_owner(endpoint, request_user)
        else:
            raise PermissionDenied("can't post to box %s" % box)

        message = self.perform_create(data)
        return message

    def perform_create(self, data):
        box = data.get("box")
        endpoint_id = data.get("endpoint_id")
        text = data.get("text")
        sender = data.get("sender")
        recipient = data.get("recipient")

        computed_hash = hash_message(text, sender, recipient)
        requested_hash = data.get("message_hash")
        if requested_hash is not None and requested_hash != computed_hash:
            raise ValidationError("hash mismatch")
        return models.Message.objects.create(
                sender=sender, recipient=recipient,
                text=text, message_hash=computed_hash,
                endpoint_id=endpoint_id, box=box)


def hash_message(text, sender, recipient):
    hasher = hashlib.sha256()
    hasher.update(text)
    hasher.update(sender)
    hasher.update(recipient)
    return hasher.hexdigest()


def check_cycle_is_open(endpoint):
    if endpoint.status != models.CycleStatus.OPEN:
        raise PermissionDenied("Cycle %s is not open" % endpoint.endpoint_id)


def check_cycle_can_process(endpoint):
    if endpoint.status != models.CycleStatus.CLOSED:
        raise PermissionDenied("Cycle %s is not closed" % endpoint.endpoint_id)


def verify(mixnet_data, signature, public=None):
    return backend.verify(mixnet_data, signature, public)
