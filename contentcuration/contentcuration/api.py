"""
This module acts as the only interface point between other apps and the database backend for the content.
"""
import logging
import os
import re
from functools import wraps
from django.core.files import File as DjFile
from django.db.models import Q, Value
from django.db.models.functions import Concat
from django.core.exceptions import ObjectDoesNotExist
from django.http import HttpResponse
from kolibri.content import models as KolibriContent
from django.db import transaction
from le_utils.constants import content_kinds
import contentcuration.models as models

def recurse(node, level=0):
    print ('\t' * level), node.id, node.lft, node.rght, node.title
    for child in ContentNode.objects.filter(parent=node).order_by('sort_order'):
        recurse(child, level + 1)

def clean_db():
    logging.debug("*********** CLEANING DATABASE ***********")
    for file_obj in models.File.objects.filter(Q(preset = None) & Q(contentnode=None)):
        logging.debug("Deletng unreferenced file {0}".format(file_obj.__dict__))
        file_obj.delete()
    for node_obj in models.ContentNode.objects.filter(Q(parent=None) & Q(channel_main=None) & Q(channel_trash=None) & Q(user_clipboard=None)):
        logging.debug("Deletng unreferenced node: {0}".format(node_obj.pk))
        node_obj.delete()
    for tag_obj in models.ContentTag.objects.filter(tagged_content=None):
        logging.debug("Deleting unreferenced tag: {0}".format(tag_obj.tag_name))
        tag_obj.delete()
    logging.debug("*********** DONE ***********")

def calculate_node_metadata(node):
    metadata = {
        "total_count" : node.children.count(),
        "resource_count" : 0,
        "max_sort_order" : 1,
        "resource_size" : 0,
        "has_changed_descendant" : node.changed
    }

    if node.kind_id == "topic":
        for n in node.children.all():
            metadata['max_sort_order'] = max(n.sort_order, metadata['max_sort_order'])
            child_metadata = calculate_node_metadata(n)
            metadata['total_count'] += child_metadata['total_count']
            metadata['resource_size'] += child_metadata['resource_size']
            metadata['resource_count'] += child_metadata['resource_count']
            metadata['has_changed_descendant'] = metadata['has_changed_descendant'] or child_metadata['has_changed_descendant']

    else:
        metadata['resource_count'] = 1
        for f in node.files.values_list('file_size'):
            metadata['resource_size'] += f[0]
        metadata['max_sort_order'] = node.sort_order

    return metadata

def count_files(node):
    if node.kind_id == "topic":
        count = 0
        for n in node.children.all():
            count += count_files(n)
        return count
    return 1

def count_all_children(node):
    count = node.children.count()
    for n in node.children.all():
        count += count_all_children(n)
    return count

def get_total_size(node):
    total_size = 0
    if node.kind_id == "topic":
        for n in node.children.all():
            total_size += get_total_size(n)
    else:
        for f in node.files.all():
            total_size += f.file_size
    return total_size

def get_node_siblings(node):
    siblings = []
    for n in node.get_siblings(include_self=False):
        siblings.append(n.title)
    return siblings

def get_node_ancestors(node):
    ancestors = []
    for n in node.get_ancestors():
        ancestors.append(n.id)
    return ancestors

def get_child_names(node):
    names = []
    for n in node.get_children():
        names.append({"title": n.title, "id" : n.id})
    return names

def batch_add_tags(request):
    # check existing tag and subtract them from bulk_create
    insert_list = []
    tag_names = request.POST.getlist('tags[]')
    existing_tags = models.ContentTag.objects.filter(tag_name__in=tag_names)
    existing_tag_names = existing_tags.values_list('tag_name', flat=True)
    new_tag_names = set(tag_names) - set(existing_tag_names)
    for name in new_tag_names:
        insert_list.append(models.ContentTag(tag_name=name))
    new_tags = models.ContentTag.objects.bulk_create(insert_list)

    # bulk add all tags to selected nodes
    all_tags = set(existing_tags).union(set(new_tags))
    ThroughModel = models.Node.tags.through
    bulk_list = []
    node_pks = request.POST.getlist('nodes[]')
    for tag in all_tags:
        for pk in node_pks:
            bulk_list.append(ThroughModel(node_id=pk, contenttag_id=tag.pk))
    ThroughModel.objects.bulk_create(bulk_list)

    return HttpResponse("Tags are successfully saved.", status=200)

def get_file_diff(file_list):
    in_db_list = models.File.objects.annotate(filename=Concat('checksum', Value('.'),  'file_format')).filter(filename__in=file_list).values_list('filename', flat=True)
    to_return = []
    for f in list(set(file_list) - set(in_db_list)):
        file_path = models.generate_file_on_disk_name(f.split(".")[-2],f)
        # Write file if it doesn't already exist
        if not os.path.isfile(file_path):
            to_return += [f]

    return to_return


""" CHANNEL CREATE FUNCTIONS """
def api_create_channel(channel_data):
    channel = create_channel(channel_data) # Set up initial channel
    root_node = init_staging_tree(channel) # Set up initial staging tree
    return channel # Return new channel

def create_channel(channel_data):
    channel, isNew = models.Channel.objects.get_or_create(id=channel_data['id'])
    channel.name = channel_data['name']
    channel.description=channel_data['description']
    channel.thumbnail=channel_data['thumbnail']
    channel.deleted = False
    channel.save()
    return channel

def init_staging_tree(channel):
    channel.staging_tree = models.ContentNode.objects.create(title=channel.name + " staging", kind_id="topic", sort_order=0)
    channel.staging_tree.published = channel.version > 0
    channel.staging_tree.save()
    channel.save()
    return channel.staging_tree

def convert_data_to_nodes(content_data, parent_node):
    try:
        root_mapping = {}
        sort_order = 1
        with transaction.atomic():
            for node_data in content_data:
                new_node = create_node(node_data, parent_node, sort_order)
                map_files_to_node(new_node, node_data['files'])
                create_exercises(new_node, node_data['questions'])
                sort_order += 1
                root_mapping.update({node_data['node_id'] : new_node.pk})
            return root_mapping
    except KeyError as e:
        raise ObjectDoesNotExist("Error creating node: {0}".format(e.message))

def create_node(node_data, parent_node, sort_order):
    title=node_data['title']
    node_id=node_data['node_id']
    description=node_data['description']
    author = node_data['author']
    kind = models.ContentKind.objects.get(kind=node_data['kind'])
    extra_fields = node_data['extra_fields']
    license = None
    license_name = node_data['license']
    if license_name is not None:
        try:
            license = models.License.objects.get(license_name__iexact=license_name)
        except ObjectDoesNotExist:
            raise ObjectDoesNotExist("Invalid license found")

    return models.ContentNode.objects.create(
        title=title,
        kind=kind,
        node_id=node_id,
        description = description,
        author=author,
        license=license,
        parent_id = parent_node,
        extra_fields=extra_fields,
        sort_order = sort_order,
    )

def map_files_to_node(node, data):
    for file_data in data:
        file_hash = file_data['filename'].split(".")
        kind_preset = None
        if file_data['preset'] is None:
            kind_preset = models.FormatPreset.objects.filter(kind=node.kind, allowed_formats__extension__contains=file_hash[1], display=True).first()
        else:
            kind_preset = models.FormatPreset.objects.get(id=file_data['preset'])

        file_obj = models.File(
            checksum=file_hash[0],
            contentnode=node,
            file_format_id=file_hash[1],
            original_filename=file_data.get('original_filename') or '',
            source_url=file_data.get('source_url'),
            file_size = file_data['size'],
            file_on_disk=DjFile(open(models.generate_file_on_disk_name(file_hash[0], file_data['filename']), 'rb')),
            preset=kind_preset,
        )
        file_obj.save()

def map_files_to_assessment_item(question, data):
    for file_data in data:
        file_hash = file_data['filename'].split(".")
        kind_preset = models.FormatPreset.objects.get(id=file_data['preset'])

        file_obj = models.File(
            checksum=file_hash[0],
            assessment_item=question,
            file_format_id=file_hash[1],
            original_filename=file_data.get('original_filename') or 'file',
            source_url=file_data.get('source_url'),
            file_size = file_data['size'],
            file_on_disk=DjFile(open(models.generate_file_on_disk_name(file_hash[0], file_data['filename']), 'rb')),
            preset=kind_preset,
        )
        file_obj.save()

def create_exercises(node, data):
    with transaction.atomic():
        order = 0

        for question in data:
            question_obj = models.AssessmentItem(
                type = question.get('type'),
                question = question.get('question'),
                hints = question.get('hints'),
                answers = question.get('answers'),
                order = order,
                contentnode = node,
                assessment_id = question.get('assessment_id'),
                raw_data = question.get('raw_data'),
            )
            order += 1
            question_obj.save()
            map_files_to_assessment_item(question_obj, question['files'])
