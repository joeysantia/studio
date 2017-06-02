import copy
import json
import logging
import os
import re
import hashlib
import shutil
import time
import tempfile
import random
import uuid
from django.http import Http404, HttpResponse, HttpResponseBadRequest, HttpResponseRedirect
from django.views.decorators.csrf import csrf_exempt
from django.shortcuts import render, get_object_or_404, redirect, render_to_response
from django.contrib.auth.decorators import login_required
from django.conf import settings
from django.core import paginator, serializers
from django.core.cache import cache
from django.core.management import call_command
from django.core.exceptions import ObjectDoesNotExist
from django.core.context_processors import csrf
from django.db import transaction
from django.db.models import Q, Case, When, Value, IntegerField, Max, Sum, Count
from django.core.urlresolvers import reverse_lazy
from django.core.files import File as DjFile
from rest_framework.renderers import JSONRenderer
from contentcuration.api import write_file_to_storage, check_supported_browsers, add_editor_to_channel, activate_channel
from contentcuration.utils.files import extract_thumbnail_wrapper, compress_video_wrapper,  generate_thumbnail_from_node, duplicate_file
from contentcuration.models import VIEW_ACCESS, Language, Exercise, AssessmentItem, Channel, License, FileFormat, File, FormatPreset, ContentKind, ContentNode, ContentTag, User, Invitation, generate_file_on_disk_name, generate_storage_url
from contentcuration.serializers import LanguageSerializer, RootNodeSerializer, AssessmentItemSerializer, AccessibleChannelListSerializer, ChannelListSerializer, ChannelSerializer, LicenseSerializer, FileFormatSerializer, FormatPresetSerializer, ContentKindSerializer, ContentNodeSerializer, TagSerializer, UserSerializer, CurrentUserSerializer, UserChannelListSerializer, FileSerializer, InvitationSerializer
from le_utils.constants import format_presets, content_kinds, file_formats, exercises, licenses
from rest_framework.authentication import SessionAuthentication, BasicAuthentication, TokenAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from pressurecooker.videos import guess_video_preset_by_resolution, extract_thumbnail_from_video, compress_video
from pressurecooker.images import create_tiled_image
from pressurecooker.encodings import write_base64_to_file

def base(request):
    if not check_supported_browsers(request.META.get('HTTP_USER_AGENT')):
        return redirect(reverse_lazy('unsupported_browser'))
    if request.user.is_authenticated():
        return redirect('channels')
    else:
        return redirect('accounts/login')

def health(request):
    return HttpResponse("500")

def unsupported_browser(request):
    return render(request, 'unsupported_browser.html')

def unauthorized(request):
    return render(request, 'unauthorized.html')

def staging_not_found(request):
    return render(request, 'staging_not_found.html')

def get_or_set_cached_constants(constant, serializer):
    cached_data = cache.get(constant.__name__)
    if cached_data:
        return cached_data
    constant_objects = constant.objects.all()
    constant_serializer = serializer(constant_objects, many=True)
    constant_data = JSONRenderer().render(constant_serializer.data)
    cache.set(constant.__name__, constant_data, None)
    return constant_data

def channel_page(request, channel, allow_edit=False, staging=False):
    channel_serializer =  ChannelSerializer(channel)
    channel_list = Channel.objects.select_related('main_tree').prefetch_related('editors').prefetch_related('viewers')\
                            .exclude(id=channel.pk).filter(Q(deleted=False) & (Q(editors=request.user) | Q(viewers=request.user)))\
                            .annotate(is_view_only=Case(When(editors=request.user, then=Value(0)),default=Value(1),output_field=IntegerField()))\
                            .distinct().values("id", "name", "is_view_only").order_by('name')

    fileformats = get_or_set_cached_constants(FileFormat, FileFormatSerializer)
    licenses = get_or_set_cached_constants(License, LicenseSerializer)
    formatpresets = get_or_set_cached_constants(FormatPreset, FormatPresetSerializer)
    contentkinds = get_or_set_cached_constants(ContentKind, ContentKindSerializer)
    languages = get_or_set_cached_constants(Language, LanguageSerializer)

    json_renderer = JSONRenderer()

    return render(request, 'channel_edit.html', {"allow_edit":allow_edit,
                                                "staging": staging,
                                                "channel" : json_renderer.render(channel_serializer.data),
                                                "channel_id" : channel.pk,
                                                "channel_name": channel.name,
                                                "channel_list" : channel_list,
                                                "fileformat_list" : fileformats,
                                                "license_list" : licenses,
                                                "fpreset_list" : formatpresets,
                                                "ckinds_list" : contentkinds,
                                                "langs_list" : languages,
                                                "current_user" : json_renderer.render(CurrentUserSerializer(request.user).data),
                                                "preferences" : request.user.preferences,
                                            })

@login_required
@authentication_classes((SessionAuthentication, BasicAuthentication, TokenAuthentication))
@permission_classes((IsAuthenticated,))
def channel_list(request):
    if not check_supported_browsers(request.META.get('HTTP_USER_AGENT')):
        return redirect(reverse_lazy('unsupported_browser'))

    channel_list = Channel.objects.prefetch_related('editors').prefetch_related('viewers').filter(Q(deleted=False) & (Q(editors=request.user.pk) | Q(viewers=request.user.pk)))\
                    .annotate(is_view_only=Case(When(editors=request.user, then=Value(0)),default=Value(1),output_field=IntegerField()))

    channel_serializer = ChannelListSerializer(channel_list, many=True)

    return render(request, 'channel_list.html', {"channels" : JSONRenderer().render(channel_serializer.data),
                                                 "channel_name" : False,
                                                 "current_user" : JSONRenderer().render(UserChannelListSerializer(request.user).data)})

def get_user_channels(request):
    channel_list = Channel.objects.prefetch_related('editors').prefetch_related('viewers').filter(Q(deleted=False) & (Q(editors=request.user.pk) | Q(viewers=request.user.pk)))\
                    .annotate(is_view_only=Case(When(editors=request.user, then=Value(0)),default=Value(1),output_field=IntegerField()))
    channel_serializer = ChannelListSerializer(channel_list, many=True)

    return HttpResponse(JSONRenderer().render(channel_serializer.data))

def get_user_pending_channels(request):
    pending_list = Invitation.objects.select_related('channel').select_related('sender').filter(invited=request.user)
    invitation_serializer = InvitationSerializer(pending_list, many=True)

    return HttpResponse(JSONRenderer().render(invitation_serializer.data))


@login_required
@authentication_classes((SessionAuthentication, BasicAuthentication, TokenAuthentication))
@permission_classes((IsAuthenticated,))
def channel(request, channel_id):
    # Check if browser is supported
    if not check_supported_browsers(request.META.get('HTTP_USER_AGENT')):
        return redirect(reverse_lazy('unsupported_browser'))

    channel = get_object_or_404(Channel, id=channel_id, deleted=False)

    # Check user has permission to view channel
    if not channel.editors.filter(id=request.user.id).exists() and not request.user.is_admin:
        return redirect(reverse_lazy('unauthorized'))

    return channel_page(request, channel, allow_edit=True)

@login_required
@authentication_classes((SessionAuthentication, BasicAuthentication, TokenAuthentication))
@permission_classes((IsAuthenticated,))
def channel_view_only(request, channel_id):
    # Check if browser is supported
    if not check_supported_browsers(request.META.get('HTTP_USER_AGENT')):
        return redirect(reverse_lazy('unsupported_browser'))

    channel = get_object_or_404(Channel, id=channel_id, deleted=False)

    # Check user has permission to view channel
    if not channel.editors.filter(id=request.user.id).exists() and not channel.viewers.filter(id=request.user.id).exists() and not request.user.is_admin:
        return redirect(reverse_lazy('unauthorized'))

    return channel_page(request, channel)

@login_required
@authentication_classes((SessionAuthentication, BasicAuthentication, TokenAuthentication))
@permission_classes((IsAuthenticated,))
def channel_staging(request, channel_id):
    # Check if browser is supported
    if not check_supported_browsers(request.META.get('HTTP_USER_AGENT')):
        return redirect(reverse_lazy('unsupported_browser'))

    channel = get_object_or_404(Channel, id=channel_id, deleted=False)

    # Check user has permission to edit channel
    if not channel.editors.filter(id=request.user.id).exists() and not request.user.is_admin:
        return redirect(reverse_lazy('unauthorized'))

    if not channel.staging_tree:
        return redirect(reverse_lazy('staging_not_found'))

    return channel_page(request, channel, allow_edit=True, staging=True)

@csrf_exempt
def publish_channel(request):
    logging.debug("Entering the publish_channel endpoint")
    if request.method != 'POST':
        return HttpResponseBadRequest("Only POST requests are allowed on this endpoint.")
    else:
        data = json.loads(request.body)

        try:
            channel_id = data["channel_id"]
        except KeyError:
            raise ObjectDoesNotExist("Missing attribute from data: {}".format(data))

        call_command("exportchannel", channel_id)

        return HttpResponse(json.dumps({
            "success": True,
            "channel": channel_id
        }))


def accessible_channels(request):
    if request.method == 'POST':
        data = json.loads(request.body)
        accessible_list = ContentNode.objects.filter(pk__in=Channel.objects.select_related('main_tree')\
                        .filter(Q(deleted=False) & (Q(public=True) | Q(editors=request.user) | Q(viewers=request.user)))\
                        .exclude(pk=data["channel_id"]).values_list('main_tree_id', flat=True))
        return HttpResponse(JSONRenderer().render(RootNodeSerializer(accessible_list, many=True).data))

def accept_channel_invite(request):
    if request.method == 'POST':
        data = json.loads(request.body)
        invitation = Invitation.objects.get(pk=data['invitation_id'])
        channel = invitation.channel
        channel.is_view_only = invitation.share_mode == VIEW_ACCESS
        channel_serializer = ChannelListSerializer(channel)
        add_editor_to_channel(invitation)

        return HttpResponse(JSONRenderer().render(channel_serializer.data))

def activate_channel_endpoint(request):
    if request.method == 'POST':
        data = json.loads(request.body)
        channel = Channel.objects.get(pk=data['channel_id'])
        activate_channel(channel)

        return HttpResponse(json.dumps({"success": True}))

def get_staged_diff(request):
    if request.method == 'POST':
        data = json.loads(request.body)
        channel = Channel.objects.get(pk=data['channel_id'])
        main_descendants = channel.main_tree.get_descendants() if channel.main_tree else None
        updated_descendants = channel.staging_tree.get_descendants() if channel.staging_tree else None

        original_stats = main_descendants.values('kind_id').annotate(count=Count('kind_id')).order_by() if main_descendants else {}
        updated_stats = updated_descendants.values('kind_id').annotate(count=Count('kind_id')).order_by() if updated_descendants else {}

        original_file_sizes = main_descendants.aggregate(
            resource_size=Sum('files__file_size'),
            assessment_size=Sum('assessment_items__files__file_size'),
            assessment_count=Count('assessment_items'),
        ) if main_descendants else {}

        updated_file_sizes = updated_descendants.aggregate(
            resource_size=Sum('files__file_size'),
            assessment_size=Sum('assessment_items__files__file_size'),
            assessment_count=Count('assessment_items')
        ) if updated_descendants else {}

        original_file_size = (original_file_sizes.get('resource_size') or 0) + (original_file_sizes.get('assessment_size') or 0)
        updated_file_size = (updated_file_sizes.get('resource_size') or 0) + (updated_file_sizes.get('assessment_size') or 0)
        original_question_count =  original_file_sizes.get('assessment_count') or 0
        updated_question_count =  updated_file_sizes.get('assessment_count') or 0

        stats = [
            {
                "field": "Date/Time Created",
                "live": channel.main_tree.created.strftime("%x %X") if channel.main_tree else None,
                "staged": channel.staging_tree.created.strftime("%x %X") if channel.staging_tree else None,
            },
            {
                "field": "File Size",
                "live": original_file_size,
                "staged": updated_file_size,
                "difference": updated_file_size - original_file_size,
                "format_size": True,
            },
        ]

        for kind, name in content_kinds.choices:
            original = original_stats.get(kind_id=kind)['count'] if original_stats.filter(kind_id=kind).exists() else 0
            updated = updated_stats.get(kind_id=kind)['count'] if updated_stats.filter(kind_id=kind).exists() else 0
            stats.append({ "field": "# of {}s".format(name), "live": original, "staged": updated, "difference": updated - original })

        # Add number of questions
        stats.append({
            "field": "# of Questions",
            "live": original_question_count,
            "staged": updated_question_count,
            "difference": updated_question_count - original_question_count,
        });

        # Add number of subtitles
        original_subtitle_count = main_descendants.filter(files__preset_id=format_presets.VIDEO_SUBTITLE).count() if main_descendants else 0
        updated_subtitle_count = updated_descendants.filter(files__preset_id=format_presets.VIDEO_SUBTITLE).count() if updated_descendants else 0
        stats.append({
            "field": "# of Subtitles",
            "live": original_subtitle_count,
            "staged": updated_subtitle_count,
            "difference": updated_subtitle_count - original_subtitle_count,
        });

        return HttpResponse(json.dumps(stats))