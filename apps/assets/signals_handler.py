# -*- coding: utf-8 -*-
#
from operator import add, sub
from functools import partial

from django.db.models.signals import (
    post_save, m2m_changed
)
from django.db.models.aggregates import Count
from django.db.models import Q, F, Model
from django.dispatch import receiver

from common.const.signals import PRE_ADD, POST_ADD, POST_REMOVE, PRE_CLEAR
from common.utils import get_logger
from common.decorator import on_transaction_commit
from .models import Asset, SystemUser, Node
from users.models import User
from .tasks import (
    update_assets_hardware_info_util,
    test_asset_connectivity_util,
    push_system_user_to_assets_manual,
    push_system_user_to_assets,
    add_nodes_assets_to_system_users
)


logger = get_logger(__file__)


def update_asset_hardware_info_on_created(asset):
    logger.debug("Update asset `{}` hardware info".format(asset))
    update_assets_hardware_info_util.delay([asset])


def test_asset_conn_on_created(asset):
    logger.debug("Test asset `{}` connectivity".format(asset))
    test_asset_connectivity_util.delay([asset])


@receiver(post_save, sender=Asset)
@on_transaction_commit
def on_asset_created_or_update(sender, instance=None, created=False, **kwargs):
    """
    当资产创建时，更新硬件信息，更新可连接性
    确保资产必须属于一个节点
    """
    if created:
        logger.info("Asset create signal recv: {}".format(instance))

        # 获取资产硬件信息
        update_asset_hardware_info_on_created(instance)
        test_asset_conn_on_created(instance)

        # 确保资产存在一个节点
        has_node = instance.nodes.all().exists()
        if not has_node:
            instance.nodes.add(Node.org_root())


@receiver(post_save, sender=SystemUser, dispatch_uid="jms")
def on_system_user_update(sender, instance=None, created=True, **kwargs):
    """
    当系统用户更新时，可能更新了秘钥，用户名等，这时要自动推送系统用户到资产上,
    其实应该当 用户名，密码，秘钥 sudo等更新时再推送，这里偷个懒,
    这里直接取了 instance.assets 因为nodes和系统用户发生变化时，会自动将nodes下的资产
    关联到上面
    """
    if instance and not created:
        logger.info("System user update signal recv: {}".format(instance))
        assets = instance.assets.all().valid()
        push_system_user_to_assets.delay(instance, assets)


@receiver(m2m_changed, sender=SystemUser.assets.through)
def on_system_user_assets_change(sender, instance=None, action='', model=None, pk_set=None, **kwargs):
    """
    当系统用户和资产关系发生变化时，应该重新推送系统用户到新添加的资产中
    """
    if action != POST_ADD:
        return
    logger.debug("System user assets change signal recv: {}".format(instance))
    queryset = model.objects.filter(pk__in=pk_set)
    if model == Asset:
        system_users = [instance]
        assets = queryset
    else:
        system_users = queryset
        assets = [instance]
    for system_user in system_users:
        push_system_user_to_assets.delay(system_user, assets)


@receiver(m2m_changed, sender=SystemUser.users.through)
def on_system_user_users_change(sender, instance=None, action='', model=None, pk_set=None, **kwargs):
    """
    当系统用户和用户关系发生变化时，应该重新推送系统用户资产中
    """
    if action != POST_ADD:
        return
    if not instance.username_same_with_user:
        return
    logger.debug("System user users change signal recv: {}".format(instance))
    queryset = model.objects.filter(pk__in=pk_set)
    if model == SystemUser:
        system_users = queryset
    else:
        system_users = [instance]
    for s in system_users:
        push_system_user_to_assets_manual.delay(s)


@receiver(m2m_changed, sender=SystemUser.nodes.through)
def on_system_user_nodes_change(sender, instance=None, action=None, model=None, pk_set=None, **kwargs):
    """
    当系统用户和节点关系发生变化时，应该将节点下资产关联到新的系统用户上
    """
    if action != POST_ADD:
        return
    logger.info("System user nodes update signal recv: {}".format(instance))

    queryset = model.objects.filter(pk__in=pk_set)
    if model == Node:
        nodes_keys = queryset.values_list('key', flat=True)
        system_users = [instance]
    else:
        nodes_keys = [instance.key]
        system_users = queryset
    add_nodes_assets_to_system_users.delay(nodes_keys, system_users)


@receiver(m2m_changed, sender=SystemUser.groups.through)
def on_system_user_groups_change(instance, action, pk_set, reverse, **kwargs):
    """
    当系统用户和用户组关系发生变化时，应该将组下用户关联到新的系统用户上
    """
    if action != POST_ADD:
        return
    logger.info("System user groups update signal recv: {}".format(instance))
    if reverse:
        system_user_pk_set = [instance.id]
        group_pk_set = pk_set
    else:
        system_user_pk_set = pk_set
        group_pk_set = [instance.id]

    user_pk_set = User.objects.filter(
        groups__in=group_pk_set
    ).distinct().values_list('id', flat=True)

    m2m_model = SystemUser.users.through
    exist = set(m2m_model.objects.filter(
        systemuser_id__in=system_user_pk_set,
        user_id__in=user_pk_set
    ).values_list('systemuser_id', 'user_id'))

    to_create = []
    for system_user_pk in system_user_pk_set:
        for user_pk in user_pk_set:
            if (system_user_pk, user_pk) in exist:
                continue
            to_create.append(m2m_model(
                systemuser_id=system_user_pk,
                user_id=user_pk
            ))
    m2m_model.objects.bulk_create(to_create)


@receiver(m2m_changed, sender=Asset.nodes.through)
def on_asset_nodes_add(instance, action, reverse, pk_set, **kwargs):
    """
    本操作共访问 4 次数据库

    当资产的节点发生变化时，或者 当节点的资产关系发生变化时，
    节点下新增的资产，添加到节点关联的系统用户中
    """
    if action != POST_ADD:
        return
    logger.debug("Assets node add signal recv: {}".format(action))
    if reverse:
        nodes = [instance.key]
        asset_ids = pk_set
    else:
        nodes = Node.objects.filter(pk__in=pk_set).values_list('key', flat=True)
        asset_ids = [instance.id]

    # 节点资产发生变化时，将资产关联到节点及祖先节点关联的系统用户, 只关注新增的
    nodes_ancestors_keys = set()
    for node in nodes:
        nodes_ancestors_keys.update(Node.get_node_ancestor_keys(node, with_self=True))

    # 查询所有祖先节点关联的系统用户，都是要跟资产建立关系的
    system_user_ids = SystemUser.objects.filter(
        nodes__key__in=nodes_ancestors_keys
    ).distinct().values_list('id', flat=True)

    # 查询所有已存在的关系
    m2m_model = SystemUser.assets.through
    exist = set(m2m_model.objects.filter(
        systemuser_id__in=system_user_ids, asset_id__in=asset_ids
    ).values_list('systemuser_id', 'asset_id'))

    to_create = []

    for system_user_id in system_user_ids:
        for asset_id in asset_ids:
            if (system_user_id, asset_id) in exist:
                continue
            to_create.append(m2m_model(
                systemuser_id=system_user_id,
                asset_id=asset_id
            ))
    m2m_model.objects.bulk_create(to_create)


def _update_node_assets_amount(node: Node, asset_pk_set: set, operator=add):
    """
    :param asset_pk_set: 内部不会修改该值
    """
    ancestor_keys = node.get_ancestor_keys(with_self=True)
    for ancestor_key in ancestor_keys:
        # 祖先节点的顺序是从下往上的
        asset_pk_set -= set(Asset.objects.filter(
            id__in=asset_pk_set
        ).filter(
            Q(nodes__key__startswith=f'{ancestor_key}:') |
            Q(nodes__key=ancestor_key)
        ).distinct().values_list('id', flat=True))
        if not asset_pk_set:
            # 该节点包含所有要增加的资产，它及其祖先都不用更新资产数
            break
        Node.objects.filter(
            key=ancestor_key
        ).update(assets_amount=operator(F('assets_amount'), len(asset_pk_set)))


@receiver(m2m_changed, sender=Asset.nodes.through)
def update_nodes_assets_amount(action, instance, reverse, pk_set, **kwargs):
    refused = (PRE_CLEAR,)
    if action in refused:
        raise ValueError

    mapper = {
        PRE_ADD: add,
        POST_REMOVE: sub
    }
    if action not in mapper:
        return

    _update = partial(_update_node_assets_amount, operator=mapper[action])

    if reverse:
        node: Node = instance
        asset_pk_set = set(pk_set)
        _update(node, asset_pk_set)
    else:
        asset_pk_set = set(instance.id)
        nodes = Node.objects.filter(id__in=pk_set)
        for node in nodes:
            _update(node, asset_pk_set)
