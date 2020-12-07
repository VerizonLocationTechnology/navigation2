#! /usr/bin/env python3

# Copyright (c) 2018 Intel Corporation.
# Copyright (c) 2020 Samsung Research Russia
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import math
import sys
import time
from typing import Optional

from action_msgs.msg import GoalStatus
from geometry_msgs.msg import Pose
from geometry_msgs.msg import PoseStamped
from geometry_msgs.msg import PoseWithCovarianceStamped
from lifecycle_msgs.srv import GetState
from nav2_msgs.action import NavigateToPose
from nav2_msgs.srv import ManageLifecycleNodes
from nav_msgs.msg import OccupancyGrid
from nav_msgs.msg import Path

import rclpy

from rclpy.action import ActionClient
from rclpy.node import Node
from rclpy.qos import QoSDurabilityPolicy, QoSHistoryPolicy, QoSReliabilityPolicy
from rclpy.qos import QoSProfile


class FilterMask():

    def __init__(
        self,
        filter_mask: OccupancyGrid
    ):
        self.filter_mask = filter_mask

    # Converts world coordinates into filter mask map coordinate.
    # Returns filter mask map coordinates or (-1, -1) in case
    # if world coordinates are out of mask bounds.
    def worldToMap(self, wx: float, wy: float):
        origin_x = self.filter_mask.info.origin.position.x
        origin_y = self.filter_mask.info.origin.position.y
        size_x = self.filter_mask.info.width
        size_y = self.filter_mask.info.height
        resolution = self.filter_mask.info.resolution

        if wx < origin_x or wy < origin_y:
            return -1, -1

        mx = int((wx - origin_x) / resolution)
        my = int((wy - origin_y) / resolution)

        if mx < size_x and my < size_y:
            return mx, my

        return -1, -1

    # Gets filter_mask[mx, my] value
    def getValue(self, mx, my):
        size_x = self.filter_mask.info.width
        return self.filter_mask.data[mx + my * size_x]


class NavTester(Node):

    def __init__(
        self,
        initial_pose: Pose,
        goal_pose: Pose,
        namespace: str = ''
    ):
        super().__init__(node_name='nav2_tester', namespace=namespace)
        self.initial_pose_pub = self.create_publisher(PoseWithCovarianceStamped,
                                                      'initialpose', 10)
        self.goal_pub = self.create_publisher(PoseStamped, 'goal_pose', 10)

        pose_qos = QoSProfile(
          durability=QoSDurabilityPolicy.RMW_QOS_POLICY_DURABILITY_TRANSIENT_LOCAL,
          reliability=QoSReliabilityPolicy.RMW_QOS_POLICY_RELIABILITY_RELIABLE,
          history=QoSHistoryPolicy.RMW_QOS_POLICY_HISTORY_KEEP_LAST,
          depth=1)

        path_qos = QoSProfile(
          durability=QoSDurabilityPolicy.RMW_QOS_POLICY_DURABILITY_VOLATILE,
          reliability=QoSReliabilityPolicy.RMW_QOS_POLICY_RELIABILITY_RELIABLE,
          history=QoSHistoryPolicy.RMW_QOS_POLICY_HISTORY_KEEP_LAST,
          depth=1)

        self.model_pose_sub = self.create_subscription(PoseWithCovarianceStamped,
                                                       'amcl_pose', self.poseCallback, pose_qos)

        self.filter_test_result = True
        self.plan_sub = self.create_subscription(Path, 'plan',
                                                 self.planCallback, path_qos)

        self.mask_received = False
        self.mask_sub = self.create_subscription(OccupancyGrid, 'filter_mask',
                                                 self.maskCallback, pose_qos)

        self.initial_pose_received = False
        self.initial_pose = initial_pose
        self.goal_pose = goal_pose
        self.action_client = ActionClient(self, NavigateToPose, 'navigate_to_pose')

    def info_msg(self, msg: str):
        self.get_logger().info('\033[1;37;44m' + msg + '\033[0m')

    def warn_msg(self, msg: str):
        self.get_logger().warn('\033[1;37;43m' + msg + '\033[0m')

    def error_msg(self, msg: str):
        self.get_logger().error('\033[1;37;41m' + msg + '\033[0m')

    def setInitialPose(self):
        msg = PoseWithCovarianceStamped()
        msg.pose.pose = self.initial_pose
        msg.header.frame_id = 'map'
        self.info_msg('Publishing Initial Pose')
        self.initial_pose_pub.publish(msg)
        self.currentPose = self.initial_pose

    def getStampedPoseMsg(self, pose: Pose):
        msg = PoseStamped()
        msg.header.frame_id = 'map'
        msg.pose = pose
        return msg

    def publishGoalPose(self, goal_pose: Optional[Pose] = None):
        self.goal_pose = goal_pose if goal_pose is not None else self.goal_pose
        self.goal_pub.publish(self.getStampedPoseMsg(self.goal_pose))

    def runNavigateAction(self, goal_pose: Optional[Pose] = None):
        # Sends a `NavToPose` action request and waits for completion
        self.info_msg("Waiting for 'NavigateToPose' action server")
        while not self.action_client.wait_for_server(timeout_sec=1.0):
            self.info_msg("'NavigateToPose' action server not available, waiting...")

        self.goal_pose = goal_pose if goal_pose is not None else self.goal_pose
        goal_msg = NavigateToPose.Goal()
        goal_msg.pose = self.getStampedPoseMsg(self.goal_pose)

        self.info_msg('Sending goal request...')
        send_goal_future = self.action_client.send_goal_async(goal_msg)

        rclpy.spin_until_future_complete(self, send_goal_future)
        goal_handle = send_goal_future.result()

        if not goal_handle.accepted:
            self.error_msg('Goal rejected')
            return False

        self.info_msg('Goal accepted')
        get_result_future = goal_handle.get_result_async()

        self.info_msg("Waiting for 'NavigateToPose' action to complete")
        rclpy.spin_until_future_complete(self, get_result_future)
        status = get_result_future.result().status
        if status != GoalStatus.STATUS_SUCCEEDED:
            self.info_msg('Goal failed with status code: {0}'.format(status))
            return False

        self.info_msg('Goal succeeded!')
        return True

    def isInKeepout(self, x, y):
        mx, my = self.filter_mask.worldToMap(x, y)
        if mx == -1 and my == -1:  # Out of mask's area
            return False
        if self.filter_mask.getValue(mx, my) == 100:  # Occupied
            return True
        return False

    def checkKeepout(self, x, y):
        if not self.mask_received:
            self.warn_msg('Filter mask was not received')
        elif self.isInKeepout(x, y):
            self.filter_test_result = False
            self.error_msg('Pose (' + str(x) + ', ' + str(y) + ') belongs to keepout zone')
            return False
        return True

    def poseCallback(self, msg):
        self.info_msg('Received amcl_pose')
        self.current_pose = msg.pose.pose
        self.initial_pose_received = True
        if not self.checkKeepout(msg.pose.pose.position.x, msg.pose.pose.position.y):
            self.error_msg('Robot goes into keepout zone')

    def planCallback(self, msg):
        self.info_msg('Received plan')
        for pose in msg.poses:
            if not self.checkKeepout(pose.pose.position.x, pose.pose.position.y):
                self.error_msg('Path plan intersects with keepout zone')
                return

    def maskCallback(self, msg):
        self.info_msg('Received filter mask')
        self.filter_mask = FilterMask(msg)
        self.mask_received = True

    def wait_for_filter_mask(self, timeout):
        start_time = time.time()

        while not self.mask_received:
            self.info_msg('Waiting for filter mask to be received ...')
            rclpy.spin_once(self, timeout_sec=1)
            if (time.time() - start_time) > timeout:
                self.error_msg('Time out to waiting filter mask')
                return False
        return True

    def reachesGoal(self, timeout, distance):
        goalReached = False
        start_time = time.time()

        while not goalReached:
            rclpy.spin_once(self, timeout_sec=1)
            if self.distanceFromGoal() < distance:
                goalReached = True
                self.info_msg('*** GOAL REACHED ***')
                return True
            elif timeout is not None:
                if (time.time() - start_time) > timeout:
                    self.error_msg('Robot timed out reaching its goal!')
                    return False

    def distanceFromGoal(self):
        d_x = self.current_pose.position.x - self.goal_pose.position.x
        d_y = self.current_pose.position.y - self.goal_pose.position.y
        distance = math.sqrt(d_x * d_x + d_y * d_y)
        self.info_msg('Distance from goal is: ' + str(distance))
        return distance

    def wait_for_node_active(self, node_name: str):
        # Waits for the node within the tester namespace to become active
        self.info_msg('Waiting for ' + node_name + ' to become active')
        node_service = node_name + '/get_state'
        state_client = self.create_client(GetState, node_service)
        while not state_client.wait_for_service(timeout_sec=1.0):
            self.info_msg(node_service + ' service not available, waiting...')
        req = GetState.Request()  # empty request
        state = 'UNKNOWN'
        while (state != 'active'):
            self.info_msg('Getting ' + node_name + ' state...')
            future = state_client.call_async(req)
            rclpy.spin_until_future_complete(self, future)
            if future.result() is not None:
                state = future.result().current_state.label
                self.info_msg('Result of get_state: %s' % state)
            else:
                self.error_msg('Exception while calling service: %r' % future.exception())
            time.sleep(5)

    def shutdown(self):
        self.info_msg('Shutting down')
        self.action_client.destroy()

        transition_service = 'lifecycle_manager_navigation/manage_nodes'
        mgr_client = self.create_client(ManageLifecycleNodes, transition_service)
        while not mgr_client.wait_for_service(timeout_sec=1.0):
            self.info_msg(transition_service + ' service not available, waiting...')

        req = ManageLifecycleNodes.Request()
        req.command = ManageLifecycleNodes.Request().SHUTDOWN
        future = mgr_client.call_async(req)
        try:
            self.info_msg('Shutting down navigation lifecycle manager...')
            rclpy.spin_until_future_complete(self, future)
            future.result()
            self.info_msg('Shutting down navigation lifecycle manager complete.')
        except Exception as e:
            self.error_msg('Service call failed %r' % (e,))
        transition_service = 'lifecycle_manager_localization/manage_nodes'
        mgr_client = self.create_client(ManageLifecycleNodes, transition_service)
        while not mgr_client.wait_for_service(timeout_sec=1.0):
            self.info_msg(transition_service + ' service not available, waiting...')

        req = ManageLifecycleNodes.Request()
        req.command = ManageLifecycleNodes.Request().SHUTDOWN
        future = mgr_client.call_async(req)
        try:
            self.info_msg('Shutting down localization lifecycle manager...')
            rclpy.spin_until_future_complete(self, future)
            future.result()
            self.info_msg('Shutting down localization lifecycle manager complete')
        except Exception as e:
            self.error_msg('Service call failed %r' % (e,))

    def wait_for_initial_pose(self):
        self.initial_pose_received = False
        while not self.initial_pose_received:
            self.info_msg('Setting initial pose')
            self.setInitialPose()
            self.info_msg('Waiting for amcl_pose to be received')
            rclpy.spin_once(self, timeout_sec=1)


def test_RobotMovesToGoal(robot_tester):
    robot_tester.info_msg('Setting goal pose')
    robot_tester.publishGoalPose()
    robot_tester.info_msg('Waiting 60 seconds for robot to reach goal')
    return robot_tester.reachesGoal(timeout=60, distance=0.5)


def run_all_tests(robot_tester):
    # set transforms to use_sim_time
    result = True
    if (result):
        robot_tester.wait_for_node_active('amcl')
        robot_tester.wait_for_initial_pose()
        robot_tester.wait_for_node_active('bt_navigator')
        result = robot_tester.wait_for_filter_mask(10)

    if (result):
        result = robot_tester.runNavigateAction()

    if (result):
        result = test_RobotMovesToGoal(robot_tester)

    if (result):
        result = robot_tester.filter_test_result

    # Add more tests here if desired

    if (result):
        robot_tester.info_msg('Test PASSED')
    else:
        robot_tester.error_msg('Test FAILED')

    return result


def fwd_pose(x=0.0, y=0.0, z=0.01):
    initial_pose = Pose()
    initial_pose.position.x = x
    initial_pose.position.y = y
    initial_pose.position.z = z
    initial_pose.orientation.x = 0.0
    initial_pose.orientation.y = 0.0
    initial_pose.orientation.z = 0.0
    initial_pose.orientation.w = 1.0
    return initial_pose


def get_tester(args):

    # Requested tester for one robot
    init_x, init_y, final_x, final_y = args.robot[0]
    tester = NavTester(
        initial_pose=fwd_pose(float(init_x), float(init_y)),
        goal_pose=fwd_pose(float(final_x), float(final_y)))
    tester.info_msg(
        'Starting tester, robot going from ' + init_x + ', ' + init_y +
        ' to ' + final_x + ', ' + final_y + '.')
    return tester


def main(argv=sys.argv[1:]):
    # The robot(s) positions from the input arguments
    parser = argparse.ArgumentParser(description='System-level navigation tester node')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('-r', '--robot', action='append', nargs=4,
                       metavar=('init_x', 'init_y', 'final_x', 'final_y'),
                       help='The robot starting and final positions.')

    args, unknown = parser.parse_known_args()

    rclpy.init()

    # Create tester for the robot
    tester = get_tester(args)

    # wait a few seconds to make sure entire stacks are up
    time.sleep(10)

    passed = run_all_tests(tester)

    # stop and shutdown the nav stack to exit cleanly
    tester.shutdown()
    tester.info_msg('Done Shutting Down.')

    if not passed:
        tester.info_msg('Exiting failed')
        exit(1)
    else:
        tester.info_msg('Exiting passed')
        exit(0)


if __name__ == '__main__':
    main()