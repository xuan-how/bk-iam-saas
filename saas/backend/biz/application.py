# -*- coding: utf-8 -*-
"""
TencentBlueKing is pleased to support the open source community by making 蓝鲸智云-权限中心(BlueKing-IAM) available.
Copyright (C) 2017-2021 THL A29 Limited, a Tencent company. All rights reserved.
Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
You may obtain a copy of the License at http://opensource.org/licenses/MIT
Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
specific language governing permissions and limitations under the License.
"""
import logging
from abc import ABC, abstractmethod
from collections import defaultdict
from copy import deepcopy
from itertools import groupby
from typing import Any, Dict, List, Optional, Tuple

from django.db import transaction
from django.utils.translation import gettext as _
from pydantic import BaseModel
from pydantic.tools import parse_obj_as
from rest_framework.request import Request

from backend.apps.application.models import Application
from backend.apps.group.models import Group
from backend.apps.organization.models import User
from backend.apps.policy.models import Policy
from backend.apps.role.models import Role
from backend.apps.template.models import PermTemplatePolicyAuthorized
from backend.biz.resource import ResourceBiz, ResourceNodeAttributeDictBean, ResourceNodeBean
from backend.common.error_codes import error_codes
from backend.common.time import expired_at_display
from backend.service.application import ApplicationService
from backend.service.approval import ApprovalProcessService
from backend.service.constants import ANY_ID, ApplicationStatus, ApplicationTypeEnum, RoleType, SubjectType
from backend.service.models import (
    ApplicantDepartment,
    ApplicantInfo,
    ApplicationAuthorizationScope,
    ApplicationGroupInfo,
    ApplicationGroupPermTemplate,
    ApplicationPolicyInfo,
    ApplicationSubject,
    ApplicationSystem,
    ApprovalProcess,
    ApprovalProcessNodeWithProcessor,
    ApprovalProcessWithNodeProcessor,
    GradeManagerApplicationContent,
    GradeManagerApplicationData,
    GrantActionApplicationContent,
    GrantActionApplicationData,
    GroupApplicationContent,
    GroupApplicationData,
    Subject,
)
from backend.service.role import RoleService
from backend.service.system import SystemService
from backend.util.cache import region

from .group import GroupBiz, GroupMemberExpiredAtBean
from .policy import (
    ConditionBean,
    InstanceBean,
    PolicyBean,
    PolicyBeanList,
    PolicyEmptyException,
    PolicyOperationBiz,
    PolicyQueryBiz,
    RelatedResourceBean,
)
from .role import RoleBiz, RoleInfo, RoleInfoBean
from .subject import SubjectInfoList
from .template import TemplateBiz

logger = logging.getLogger(__name__)


class BaseApplicationDataBean(BaseModel):
    """申请的基本数据"""

    applicant: str
    reason: str


class ActionApplicationDataBean(BaseApplicationDataBean):
    """自定义权限申请或续期"""

    policy_list: PolicyBeanList

    class Config:
        # 由于PolicyBeanList并非继承于BaseModel，而是普通的类，所以需要声明允许"随意"类型
        arbitrary_types_allowed = True


class ApplicationRenewPolicyInfoBean(BaseModel):
    """自定义权限续期的策略信息"""

    id: int
    expired_at: int


class ApplicationGroupInfoBean(BaseModel):
    """申请用户组的信息"""

    id: int
    # 申请加入用户组的有效期或续期有效期
    expired_at: int


class GroupApplicationDataBean(BaseApplicationDataBean):
    """用户组申请或续期"""

    groups: List[ApplicationGroupInfoBean]


class GradeManagerApplicationDataBean(BaseApplicationDataBean):
    """分级管理员创建或更新"""

    role_id: int = 0
    role_info: RoleInfo


class ApplicationIDStatusDict(BaseModel):
    data: Dict[int, ApplicationStatus]

    def get(self, _id: int) -> Optional[ApplicationStatus]:
        return self.data.get(_id)


class ApprovalProcessorBiz:
    """审批流程具体处理人查询
    当前只能查询IAM本身角色，后续需要扩展支持其他的再重新抽象该类
    """

    svc = RoleService()

    @region.cache_on_arguments(expiration_time=60)  # 缓存1分钟
    def get_super_manager_members(self) -> str:
        """获取超级管理员成员员"""
        return Role.objects.get(type=RoleType.SUPER_MANAGER.value).members

    @region.cache_on_arguments(expiration_time=60)  # 缓存1分钟
    def get_system_manager_members(self, system_id: str) -> str:
        """获取系统管理员成员"""
        return Role.objects.get(type=RoleType.SYSTEM_MANAGER.value, code=system_id).members

    @region.cache_on_arguments(expiration_time=60)  # 缓存1分钟
    def get_grade_manager_members_by_group_id(self, group_id: int) -> str:
        """获取分级管理员"""
        return self.svc.get_role_by_group_id(group_id).members


class PolicyProcess(BaseModel):
    """
    策略关联的审批流程对象

    用于自定义申请
    """

    policy: PolicyBean
    process: ApprovalProcessWithNodeProcessor


class PolicyProcesshandler(ABC):
    """
    处理policy - process的管道
    """

    @abstractmethod
    def handle(self, policy_process_list: List[PolicyProcess]) -> List[PolicyProcess]:
        pass


class InstanceAproverHandler(PolicyProcesshandler):
    """
    实例审批人处理管道
    """

    resource_biz = ResourceBiz()

    def handle(self, policy_process_list: List[PolicyProcess]) -> List[PolicyProcess]:
        # 返回的结果
        policy_process_results = []
        # 需要处理的有实例审批人节点的policy_process
        policy_process_with_approver_node = []
        # 需要查询实例审批人的资源实例
        resource_nodes = set()

        for policy_process in policy_process_list:
            # 没有实例审批人节点不需要处理
            if not policy_process.process.has_instance_approver_node():
                policy_process_results.append(policy_process)
                continue

            # 保存需要处理实例审批人的policy_process
            policy_process_with_approver_node.append(policy_process)
            # 筛选出需要查询实例审批人的资源实例
            for resource_node in self._list_approver_resource_node_by_policy(policy_process.policy):
                resource_nodes.add(resource_node)

        # 没有需要查询资源审批人的实例节点
        if not resource_node:
            return policy_process_list

        # 查询资源实例的审批人
        resource_approver_dict = self.resource_biz.fetch_resource_approver(list(resource_nodes))
        if resource_approver_dict.is_empty():
            return policy_process_list

        # 依据实例审批人信息, 拆分policy, 添加到结果中
        for policy_process in policy_process_with_approver_node:
            policy_process_results.extend(
                self._split_policy_process_by_resource_approver_dict(policy_process, resource_approver_dict)
            )

        return policy_process_results

    def _split_policy_process_by_resource_approver_dict(
        self, policy_process: PolicyProcess, resource_approver_dict: ResourceNodeAttributeDictBean
    ) -> List[PolicyProcess]:
        """
        通过实例审批人信息, 分离policy_process为独立的实例policy
        """
        if len(policy_process.policy.related_resource_types) != 1:
            return [policy_process]

        policy = policy_process.policy
        process = policy_process.process
        rrt = policy.related_resource_types[0]

        policy_process_list: List[PolicyProcess] = []
        for condition in rrt.condition:
            # 忽略有属性的condition
            if not condition.has_no_attributes():
                continue

            # 遍历所有的实例路径, 筛选出有查询有实例审批人的实例
            for instance in condition.instances:
                for path in instance.path:
                    last_node = path[-1]
                    if last_node.id == ANY_ID:
                        if len(path) < 2:
                            continue
                        last_node = path[-2]

                    resource_node = ResourceNodeBean.parse_obj(last_node)
                    if not resource_approver_dict.get_attribute(resource_node):
                        continue

                    # 复制出单实例的policy
                    copied_policy = self._copy_policy_by_instance_path(policy, rrt, instance, path)

                    # 复制出新的审批流程, 并填充实例审批人
                    copied_process = deepcopy(process)
                    copied_process.set_instance_approver(resource_approver_dict.get_attribute(resource_node))

                    policy_process_list.append(PolicyProcess(policy=copied_policy, process=copied_process))

        # 如果没有拆分处理部分实例, 直接返回原始的policy_process
        if not policy_process_list:
            return [policy_process]

        # 把原始的策略剔除拆分的部分
        for part_policy_process in policy_process_list:
            try:
                policy_process.policy.remove_related_resource_types(part_policy_process.policy.related_resource_types)
            except PolicyEmptyException:
                # 如果原始的策略全部删完了, 直接返回拆分的部分
                return policy_process_list

        # 原始拆分后剩余的部分填回来
        policy_process_list.append(policy_process)
        return policy_process_list

    def _copy_policy_by_instance_path(self, policy, rrt, instance, path):
        # 复制出单实例的policy
        copied_policy = PolicyBean(
            related_resource_types=[
                RelatedResourceBean(
                    condition=[
                        ConditionBean(
                            attributes=[],
                            instances=[InstanceBean(path=[path], **instance.dict(exclude={"path"}))],
                        )
                    ],
                    **rrt.dict(exclude={"condition"}),
                )
            ],
            **policy.dict(exclude={"related_resource_types"}),
        )
        return copied_policy

    def _list_approver_resource_node_by_policy(self, policy: PolicyBean) -> List[ResourceNodeBean]:
        """列出policies中所有资源的节点"""
        # 需要查询资源实例审批人的节点集合
        resource_node_set = set()
        # 只支持关联1个资源类型的操作查询资源审批人
        if len(policy.related_resource_types) != 1:
            return []

        rrt = policy.related_resource_types[0]
        for path in rrt.iter_path_list(ignore_attribute=True):
            last_node = path.nodes[-1]
            if last_node.id == ANY_ID:
                if len(path.nodes) < 2:
                    continue
                last_node = path.nodes[-2]
            resource_node_set.add(ResourceNodeBean.parse_obj(last_node))
        return list(resource_node_set)


class ApprovedPassApplicationBiz:
    """审批通过处理"""

    policy_operation_biz = PolicyOperationBiz()
    group_biz = GroupBiz()
    role_biz = RoleBiz()

    def _grant_action(self, subject: Subject, data: Dict):
        """用户自定义权限授权"""
        system_id = data["system"]["id"]
        actions = data["actions"]

        policy_list = PolicyBeanList(system_id, parse_obj_as(List[PolicyBean], actions), need_ignore_path=True)
        self.policy_operation_biz.alter(system_id=system_id, subject=subject, policies=policy_list.policies)

    def _renew_action(self, subject: Subject, data: Dict):
        """用户自定义权限续期"""
        self._grant_action(subject, data)

    def _join_group(self, subject: Subject, data: Dict):
        """加入用户组"""
        # 兼容，新老数据在data都存在expired_at
        default_expired_at = data["expired_at"]
        # 加入用户组
        for group in data["groups"]:
            # 新数据才有，老数据则使用data外层的expired_at
            expired_at = group.get("expired_at", default_expired_at)
            try:
                self.group_biz.add_members(group["id"], [subject], expired_at)
            except Group.DoesNotExist:
                # 若审批通过时，用户组已经被删除，则直接忽略
                logger.info(f"group({group['id']}) has been deleted before the application is approved")

    def _renew_group(self, subject: Subject, data: Dict):
        """用户组续期"""
        for group in data["groups"]:
            self.group_biz.update_members_expired_at(
                group["id"],
                [GroupMemberExpiredAtBean(type=subject.type, id=subject.id, policy_expired_at=group["expired_at"])],
            )

    def _gen_role_info_bean(self, data: Dict) -> RoleInfoBean:
        """处理分级管理员数据"""
        # 兼容新老数据
        auth_scopes = data["authorization_scopes"]
        for scope in auth_scopes:
            # 新数据是system，没有system_id
            if "system_id" not in scope:
                scope["system_id"] = scope["system"]["id"]
        return RoleInfoBean(**data)

    def _create_rating_manager(self, subject: Subject, data: Dict):
        """创建分级管理员"""
        info = self._gen_role_info_bean(data)
        self.role_biz.create(info, subject.id)

    def _update_rating_manager(self, subject: Subject, data: Dict):
        """更新分级管理员"""
        role = Role.objects.get(type=RoleType.RATING_MANAGER.value, id=data["id"])
        info = self._gen_role_info_bean(data)
        self.role_biz.update(role, info, subject.id)

    def handle(self, application: Application):
        """审批通过处理"""
        func_name = f"_{application.type}"
        handle_func = getattr(self, func_name)

        subject = Subject(type=SubjectType.USER.value, id=application.applicant)
        handle_func(subject, application.data)


class ApplicationBiz:
    svc = ApplicationService()
    system_svc = SystemService()
    approval_process_svc = ApprovalProcessService()
    approval_processor_biz = ApprovalProcessorBiz()
    approved_pass_biz = ApprovedPassApplicationBiz()

    policy_biz = PolicyQueryBiz()
    template_biz = TemplateBiz()
    resource_biz = ResourceBiz()

    def _get_approval_process_with_node_processor(
        self, process: ApprovalProcess, **kwargs
    ) -> ApprovalProcessWithNodeProcessor:
        """获取审批流程并附带每个流程节点的实际处理人
        kwargs: 可能有system_id、group_id
        由于不同流程节点的查询具体处理人时是依赖申请内容的
        比如申请不同用户组，其分级管理员成员可能是不一样的，同样申请不同系统的权限，其系统管理员成员也不一样
        """
        nodes = self.approval_process_svc.get_process_nodes(process.id)
        # 1. 遍历每个节点，对IAM的角色进行查询对应的具体处理人
        nodes_with_processor = []
        for node in nodes:
            node_with_processor = parse_obj_as(ApprovalProcessNodeWithProcessor, node)
            # 非IAM来源，则不需要IAM关注
            if not node.is_iam_source():
                nodes_with_processor.append(node_with_processor)
                continue

            # 对于来着IAM的角色，则需要查询对应角色的成员
            processors = []
            if node.processor_type == RoleType.SUPER_MANAGER.value:
                processors = self.approval_processor_biz.get_super_manager_members()
            elif node.processor_type == RoleType.SYSTEM_MANAGER.value:
                processors = self.approval_processor_biz.get_system_manager_members(system_id=kwargs["system_id"])
            elif node.processor_type == RoleType.RATING_MANAGER.value:
                processors = self.approval_processor_biz.get_grade_manager_members_by_group_id(
                    group_id=kwargs["group_id"]
                )
            # NOTE: 由于资源实例审批人节点的逻辑涉及到复杂的拆分, 合并逻辑, 不在这里处理

            node_with_processor.processors = processors
            nodes_with_processor.append(node_with_processor)

        # 组装出流程带节点及其节点具体处理人数据
        process_with_node_processor = parse_obj_as(ApprovalProcessWithNodeProcessor, process)
        process_with_node_processor.nodes = nodes_with_processor

        return process_with_node_processor

    def _merge_application_by_approval_process(
        self, process_dict: Dict[Any, ApprovalProcessWithNodeProcessor]
    ) -> Dict[ApprovalProcessWithNodeProcessor, List]:
        """通过审批流程，合并申请单"""
        merge_process_dict = defaultdict(list)
        for obj, process in process_dict.items():
            merge_process_dict[process].append(obj)
        return merge_process_dict

    def _get_applicant_info(self, applicant: str) -> ApplicantInfo:
        """获取申请者相关信息"""
        # 查询用户的部门信息
        departments = User.objects.get(username=applicant).departments
        applicant_departments = [
            ApplicantDepartment(id=dept.id, name=dept.name, full_name=dept.full_name) for dept in departments
        ]

        return ApplicantInfo(username=applicant, organization=applicant_departments)

    @region.cache_on_arguments(expiration_time=60)  # 缓存1分钟
    def _gen_application_system(self, system_id: str) -> ApplicationSystem:
        """生成申请的系统信息"""
        system = self.system_svc.get(system_id)
        return parse_obj_as(ApplicationSystem, system)

    def create_for_policy(
        self, application_type: ApplicationTypeEnum, data: ActionApplicationDataBean
    ) -> List[Application]:
        """自定义权限"""
        # 1. 提前查询部分信息
        # (1) 对Policy里相关Name进行填充 => 调用Service层接口需要"完整"数据，用于Ticket创建和展示
        policy_list = data.policy_list
        policy_list.fill_empty_fields()
        # (2) 查询申请者信息
        applicant_info = self._get_applicant_info(data.applicant)
        # (3) 查询系统信息
        system_id = data.policy_list.system_id
        system_info = self._gen_application_system(system_id)

        # 3. 查询每个操作对应的流程
        action_ids = [p.action_id for p in policy_list.policies]
        action_processes = self.approval_process_svc.list_action_process(system_id=system_id, action_ids=action_ids)

        # 4. 生成policy - process对象列表
        policy_process_list = []
        for action_process in action_processes:
            process = self._get_approval_process_with_node_processor(action_process.process, system_id=system_id)
            policy = policy_list.get(action_process.action_id)

            policy_process_list.append(PolicyProcess(policy=policy, process=process))

        # 5. 通过管道填充可能的资源实例审批人/分级管理员审批节点的审批人
        pipeline: List[PolicyProcesshandler] = [InstanceAproverHandler()]  # NOTE: 未来需要实现分级管理员审批handler
        for pipe in pipeline:
            policy_process_list = pipe.handle(policy_process_list)

        # 6. 依据审批流程合并策略
        policy_list_process = self._merge_policies_by_approval_process(system_id, policy_process_list)

        # 7. 根据合并的单据，组装出调用Service层创建单据所需数据
        new_data_list = []
        for policy_list, process in policy_list_process:
            # 组装申请数据
            application_data = GrantActionApplicationData(
                type=application_type,
                applicant_info=applicant_info,
                reason=data.reason,
                content=GrantActionApplicationContent(
                    system=system_info,
                    policies=parse_obj_as(List[ApplicationPolicyInfo], policy_list.policies),
                ),
            )
            new_data_list.append((application_data, process))

        # 7. 循环创建申请单
        applications = []
        for _data, _process in new_data_list:
            application = self.svc.create_for_policy(_data, _process)
            applications.append(application)

        return applications

    def _merge_policies_by_approval_process(
        self, system_id, policy_process_list: List[PolicyProcess]
    ) -> List[Tuple[PolicyBeanList, ApprovalProcessWithNodeProcessor]]:
        """聚合审批流程相同的策略"""
        # 聚合相同流程所有的polices
        merge_process_dict = defaultdict(list)
        for policy_process in policy_process_list:
            merge_process_dict[policy_process.process].append(policy_process.policy)

        policy_list_process = []
        # 流程相同的策略中, 合并action_id一样的策略
        for _process, _policies in merge_process_dict.items():
            policy_list = PolicyBeanList(system_id, [])
            for p in _policies:
                policy_list.add(PolicyBeanList(system_id, [p]))

            policy_list_process.append((policy_list, _process))

        return policy_list_process

    def create_for_renew_policy(
        self, policy_infos: List[ApplicationRenewPolicyInfoBean], applicant: str, reason: str
    ) -> List[Application]:
        """自定义权限续期"""
        subject = Subject(type=SubjectType.USER.value, id=applicant)
        policy_expired_at_dict = {p.id: p.expired_at for p in policy_infos}

        # 查询策略所属系统
        db_policies = (
            Policy.objects.filter(
                subject_type=subject.type, subject_id=subject.id, policy_id__in=[p.id for p in policy_infos]
            )
            .defer("_resources")
            .order_by("system_id")
        )

        # 按系统分组
        data_list = []
        for system_id, policies in groupby(db_policies, lambda p: p.system_id):
            policy_list = self.policy_biz.query_policy_list_by_policy_ids(
                system_id, subject, [p.policy_id for p in policies]
            )

            # 由于是续期，所以需要修改续期时间
            for p in policy_list.policies:
                p.set_expired_at(policy_expired_at_dict[p.policy_id])

            data_list.append(ActionApplicationDataBean(applicant=applicant, policy_list=policy_list, reason=reason))

        # 循环创建申请单
        applications = []
        for data in data_list:
            applications.extend(self.create_for_policy(ApplicationTypeEnum.RENEW_ACTION.value, data))

        return applications

    def _gen_group_permission_data(self, group_id: int) -> List[ApplicationGroupPermTemplate]:
        """生成用户组权限数据"""
        subject = Subject(type=SubjectType.GROUP.value, id=str(group_id))

        application_templates = []

        # 查询自定义权限 涉及的系统
        system_counter_list = self.policy_biz.list_system_counter_by_subject(subject)
        # 查询自定义权限
        for system_counter in system_counter_list:
            policies = self.policy_biz.list_by_subject(system_counter.id, subject)
            application_templates.append(
                ApplicationGroupPermTemplate(
                    id=0,
                    name="自定义权限",
                    system=parse_obj_as(ApplicationSystem, system_counter),
                    policies=parse_obj_as(List[ApplicationPolicyInfo], policies),
                )
            )

        # 查询用户组权限模板权限
        authorized_query_set = PermTemplatePolicyAuthorized.objects.filter_by_subject(subject)
        # 查询模板名称
        template_ids = list(authorized_query_set.values_list("template_id", flat=True))
        template_name_dict = self.template_biz.get_template_name_dict_by_ids(template_ids)
        # 循环组织权限模板数据
        for authorized in authorized_query_set:
            policy_list = PolicyBeanList(
                system_id=authorized.system_id,
                policies=parse_obj_as(List[PolicyBean], authorized.data["actions"]),
                need_fill_empty_fields=True,
            )
            application_templates.append(
                ApplicationGroupPermTemplate(
                    id=authorized.template_id,
                    name=template_name_dict.get(authorized.template_id),
                    system=self._gen_application_system(authorized.system_id),
                    policies=parse_obj_as(List[ApplicationPolicyInfo], policy_list.policies),
                )
            )

        return application_templates

    def _gen_group_application_content(self, group_infos: List[ApplicationGroupInfoBean]) -> GroupApplicationContent:
        """生成用户组单据所需内容"""
        # 1. 用户组基本信息
        groups = Group.objects.filter(id__in=[g.id for g in group_infos])
        group_expired_at_dict = {g.id: g.expired_at for g in group_infos}
        # 2. 组装用户组相关数据
        group_infos = [
            ApplicationGroupInfo(
                id=group.id,
                name=group.name,
                description=group.description,
                expired_at=group_expired_at_dict[group.id],
                expired_display=expired_at_display(group_expired_at_dict[group.id]),
                templates=self._gen_group_permission_data(group.id),
            )
            for group in groups
        ]

        return GroupApplicationContent(groups=group_infos)

    def create_for_group(
        self, application_type: ApplicationTypeEnum, data: GroupApplicationDataBean
    ) -> List[Application]:
        """申请加入用户组"""
        # 1. 查询申请者信息
        applicant_info = self._get_applicant_info(data.applicant)

        # 2. 查询每个用户组的审批流程
        group_processes = self.approval_process_svc.list_group_process([g.id for g in data.groups])

        # 3. 实例化每个流程
        group_process_dict = {}
        for group_process in group_processes:
            process = self._get_approval_process_with_node_processor(
                group_process.process, group_id=group_process.group_id
            )
            group_process_dict[group_process.group_id] = process

        # 4. 合并单据
        merge_process_dict = self._merge_application_by_approval_process(group_process_dict)

        # 5. 根据合并的单据，组装出调用Service层创建单据所需数据
        new_data_list = []
        for process, group_ids in merge_process_dict.items():
            # 组装申请数据
            application_data = GroupApplicationData(
                type=application_type,
                applicant_info=applicant_info,
                reason=data.reason,
                content=self._gen_group_application_content([g for g in data.groups if g.id in group_ids]),
            )
            new_data_list.append((application_data, process))

        # 7. 循环创建申请单
        applications = []
        for _data, _process in new_data_list:
            application = self.svc.create_for_group(_data, _process)
            applications.append(application)

        return applications

    def _gen_grade_manager_application_content(
        self, role_info: RoleInfo, role_id: int
    ) -> GradeManagerApplicationContent:
        """生成申请单据所需内容"""
        # 成员需要显示名称
        members = SubjectInfoList([Subject(type=SubjectType.USER.value, id=m) for m in role_info.members])
        # 授权成员范围，查询相关信息
        subject_scopes = SubjectInfoList(role_info.subject_scopes)

        # 授权范围
        authorization_scopes = []
        for scope in role_info.authorization_scopes:
            system = self._gen_application_system(scope.system_id)
            policy_list = PolicyBeanList(
                system_id=scope.system_id,
                policies=parse_obj_as(List[PolicyBean], scope.actions),
                need_fill_empty_fields=True,
            )
            authorization_scopes.append(ApplicationAuthorizationScope(system=system, policies=policy_list.policies))

        return GradeManagerApplicationContent(
            id=role_id,
            name=role_info.name,
            description=role_info.description,
            members=parse_obj_as(List[ApplicationSubject], members.subjects),
            subject_scopes=parse_obj_as(List[ApplicationSubject], subject_scopes.subjects),
            authorization_scopes=authorization_scopes,
        )

    def create_for_grade_manager(
        self, application_type: ApplicationTypeEnum, data: GradeManagerApplicationDataBean
    ) -> List[Application]:
        """分级管理员"""
        # 1. 查询申请者信息
        applicant_info = self._get_applicant_info(data.applicant)

        # 2. 查询对应的审批流程(所有分级管理员的申请都使用同一个流程)
        grade_manager_process = self.approval_process_svc.get_default_process(
            ApplicationTypeEnum.CREATE_RATING_MANAGER.value
        )

        # 3. 实例化流程
        process = self._get_approval_process_with_node_processor(grade_manager_process.process)

        # 4. 组装数据并创建单据
        application = self.svc.create_for_grade_manager(
            GradeManagerApplicationData(
                type=application_type,
                applicant_info=applicant_info,
                reason=data.reason,
                content=self._gen_grade_manager_application_content(data.role_info, data.role_id),
            ),
            process,
        )

        return [application]

    def handle_application_result(self, application: Application, status: ApplicationStatus):
        """处理审批单据结果"""
        # 若还在审批中，则忽略
        if status == ApplicationStatus.PENDING.value:
            return

        with transaction.atomic():
            # 对于非审批中，都需要将单据状态更新保存
            # Note: 由于application里的data字段较大，使用save更新时相当所有字段都更新，所以需指定status字段更新
            application.status = status
            application.save(update_fields=["status", "updated_time"])

            # 审批通过，则执行相关授权等
            if status == ApplicationStatus.PASS.value:
                self.approved_pass_biz.handle(application)

    def handle_approval_callback_request(self, callback_id: str, request: Request):
        """处理审批回调请求"""
        # 获取审批回调处理的单据，包括单据号和单据状态
        ticket = self.svc.get_approval_ticket_from_callback_request(request)

        try:
            application = Application.objects.get(sn=ticket.sn, callback_id=callback_id)
        except Application.DoesNotExist:
            raise error_codes.NOT_FOUND_ERROR

        # 处理申请单结果
        self.handle_application_result(application, ticket.status)

    def query_application_approval_status(self, applications: List[Application]) -> ApplicationIDStatusDict:
        """查询申请单审批状态"""
        sn_id_dict = {a.sn: a.id for a in applications}
        tickets = self.svc.query_ticket_approval_status(list(sn_id_dict.keys()))

        return ApplicationIDStatusDict(data={sn_id_dict[t.sn]: t.status for t in tickets})

    def cancel_application(self, application: Application, operator: str):
        """撤销申请单"""
        if application.applicant != operator:
            raise error_codes.INVALID_ARGS.format(_("只有申请人能取消"))  # 只能取消自己的申请单

        # 撤销单据
        self.svc.cancel_ticket(application.sn, application.applicant)
        # 更新状态
        self.handle_application_result(application, ApplicationStatus.CANCELLED.value)

    def get_approval_url(self, application: Application) -> str:
        """查询审批URL"""
        ticket = self.svc.get_ticket(application.sn)
        return ticket.url
