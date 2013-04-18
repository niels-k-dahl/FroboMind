#/****************************************************************************
# FroboMind positionGoalActionServer.py
# Copyright (c) 2011-2013, author Leon Bonde Larsen <leon@bondelarsen.dk>
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#    * Redistributions of source code must retain the above copyright
#      notice, this list of conditions and the following disclaimer.
#    * Redistributions in binary form must reproduce the above copyright
#      notice, this list of conditions and the following disclaimer in the
#      documentation and/or other materials provided with the distribution.
#    * Neither the name FroboMind nor the
#      names of its contributors may be used to endorse or promote products
#      derived from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL <COPYRIGHT HOLDER> BE LIABLE FOR ANY
# DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
# ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#****************************************************************************/
# Change log:
# 26-Mar 2013 Leon: Changed to using tf for quaternion calculations
#                   Turned coordinate system to match odom frame
# 11-Apr 2013 Leon: Moved planner to library to make clean cut to action
#****************************************************************************/
import rospy, tf, math
import numpy as np
from simple_2d_math.vector import Vector
from nav_msgs.msg import Odometry
from geometry_msgs.msg import TwistStamped, Point
from position_control.planner import PositionPlanner
from line_control.markers import MarkerUtility
from line_control.velocity_control import Controller
from tf import TransformListener

class LinePlanner():
    """
        Control class taking line goals and generating twist messages accordingly
    """
    def __init__(self):
        # Init line
        self.rabbit_factor = 0.2
        self.line_begin = Vector(0,0)
        self.line_end = Vector(0,5)
        self.line = Vector(self.line_end[0]-self.line_begin[0],self.line_end[1]-self.line_begin[1])
        self.yaw = 0.0
        # Init control methods
        self.isNewGoalAvailable = self.empty_method()
        self.isPreemptRequested = self.empty_method()
        self.setPreempted = self.empty_method()
        
        # Get parameters from parameter server
        self.getParameters()
        
        self.point_marker = MarkerUtility("/point_marker" , self.odom_frame)
        self.line_marker = MarkerUtility("/line_marker" , self.odom_frame)
        self.pos_marker = MarkerUtility("/pos_marker" , self.odom_frame)
        
        # Init control loop
        self.controller = Controller()    
        self.distance_to_goal = 0
        self.angle_error = 0
        self.goal_angle_error = 0
        self.sp_linear = 0
        self.sp_angular = 0
        self.distance_to_line = 0
        
        # Init TF listener
        self.__listen = TransformListener()    
        
        # Init controller
        self.corrected = False
        self.rate = rospy.Rate(1/self.period)
        self.twist = TwistStamped()
        self.destination = Vector(1,0)
        self.position = Vector(1,0) # Vector from origo to current position of the robot
        self.heading = Vector(1,0) # Vector in the current direction of the robot 
        self.rabbit = Vector(1,0) # Vector from origo to the aiming point on the line
        self.rabbit_path = Vector(1,0) # Vector from current position to aiming point on the line
        self.perpendicular = Vector(1,0) # Vector from current position perpendicular to the line
        self.projection = Vector(1,0) # Projection of the position vector on the line
        self.quaternion = np.empty((4, ), dtype=np.float64)

        # Setup zone strategies
        # Zone 0 : close to line, good heading
        # Zone 1 : close to line, poor heading
        # Zone 3 : far from line
        # Zone 4 : Close to target
        self.target_area = 0.4
        self.z1_value = 1
        self.z1_lin_vel = 0.1
        self.z1_ang_vel = 0.2
        self.z1_rabbit = 0.8
        self.z1_max_distance = 0.05
        self.z1_max_angle = math.pi/36
        
        self.z2_value = 5
        self.z2_lin_vel = 0.1
        self.z2_ang_vel = 0.8
        self.z2_rabbit = 0.7
        self.z2_max_distance = 0.25
        self.z2_max_angle = math.pi/6
        
        self.z3_value = 10
        self.z3_lin_vel = 0.1
        self.z3_ang_vel = 1.2
        self.z3_rabbit = 0.6
        
        self.z_filter_size = 10
        self.zone_filter = [self.z3_value]*self.z_filter_size
        self.zone = 2
        self.z_ptr = 0
        
        # Setup Publishers and subscribers 
        if not self.use_tf :
            self.odom_sub = rospy.Subscriber(self.odometry_topic, Odometry, self.onOdometry )
        self.twist_pub = rospy.Publisher(self.cmd_vel_topic, TwistStamped)
        
    def getParameters(self):
        # Get topics and transforms
        self.cmd_vel_topic = rospy.get_param("~cmd_vel_topic","/fmSignals/cmd_vel")
        self.odom_frame = rospy.get_param("~odom_frame","/odom")
        self.odometry_topic = rospy.get_param("~odometry_topic","/fmKnowledge/odom")
        self.base_frame = rospy.get_param("~base_frame","/base_footprint")
        self.use_tf = rospy.get_param("~use_tf",True)
        
        # Get general parameters
        self.period = rospy.get_param("~period",0.1)
        self.max_linear_velocity = rospy.get_param("~max_linear_velocity",2)
        self.max_angular_velocity = rospy.get_param("~max_angular_velocity",1)
        self.max_distance_error = rospy.get_param("~max_distance_error",0.05)
        self.max_distance_from_line = rospy.get_param("~max_distance_from_line",0.3)
               
#        # Get parameters for action server
        self.retarder = rospy.get_param("~retarder",0.8)
        self.max_angle_error = rospy.get_param("~max_angle_error",math.pi/4)     
        self.max_initial_error = rospy.get_param("~max_initial_error",math.pi/18)

    def stop(self):
        # Publish a zero twist to stop the robot
        self.twist.header.stamp = rospy.Time.now()
        self.sp_angular = 0
        self.twist.twist.angular.z = 0
        self.twist_pub.publish(self.twist)
                
    def execute(self,goal):
        # Construct a vector from position goal
        self.line_begin[0] = goal.a_x
        self.line_begin[1] = goal.a_y  
        self.line_end[0] = goal.b_x
        self.line_end[1] = goal.b_y 
        self.line = Vector(self.line_end[0]-self.line_begin[0],self.line_end[1]-self.line_begin[1])
        # Temporary hack
#        self.update()
#        self.line_begin = Vector(self.position[0]+0.3,self.position[1]+0.3)
#        self.line_end = self.position.projectedOn(self.heading).unit().scale(3.0)
#        self.line = Vector(self.line_end[0]-self.line_begin[0],self.line_end[1]-self.line_begin[1])
        
        rospy.loginfo(rospy.get_name() + " Received goal: (%f,%f) to (%f,%f) ",goal.a_x,goal.a_y,goal.b_x,goal.b_y)
        self.zone_filter = [self.z3_value]*self.z_filter_size
        self.corrected = False
        
        while not rospy.is_shutdown() :
            
            # Check for new goal
            if self.isNewGoalAvailable() :
                rospy.loginfo(rospy.get_name() + "New goal is available")
                break

            # Preemption check
            if self.isPreemptRequested():
                rospy.loginfo(rospy.get_name() + "Preempt requested")
                break
            
            # Update
            self.update()
            
            # Construct rabbit vector
            self.constructRabbit()
            
            # Publish markers
            self.line_marker.updateLine( [ Point(self.line_begin[0],self.line_begin[1],0) , Point(self.line_end[0],self.line_end[1],0) ] )
            self.point_marker.updatePoint( [ Point(self.rabbit[0],self.rabbit[1],0) ] )
            self.pos_marker.updatePoint( [ Point(self.position[0],self.position[1],0) ] )
            
            # If the goal is unreached
            if self.distance_to_goal > self.max_distance_error :
                
                # Spin the loop
                self.control_loop()
                
                # Block   
                try :
                    self.rate.sleep()
                except rospy.ROSInterruptException:
                    print("Interrupted during sleep")
                    return 'preempted'
            else:
                # Succeed the action - position has been reached
                rospy.loginfo(rospy.get_name() + " Goal reached in distance: %f m",self.distance_to_goal)
                self.stop()            
                break
        
        # Return statement
        if self.isPreemptRequested() :
            self.setPreempted()
            print("Returning due to preemption")
            return 'preempted' 
        elif rospy.is_shutdown() :
            self.setPreempted()
            print("Returning due to abort")
            return 'aborted'
        else :   
            self.setSucceeded()
            print("Returning due to success")            
            return 'succeeded'     

    def update(self):
        # Get current position
        self.get_current_position()
        
        # Construct heading vector
        self.heading = Vector(math.cos(self.yaw), math.sin(self.yaw))
        self.controller.setFeedback(self.position,self.heading) 
        
        self.projection = (self.position - self.line_begin).projectedOn(self.line)
        if self.projection.angle(self.line) > 0.1 or self.projection.angle(self.line) < -0.1 :
            self.projection = self.projection.scale(-1)
        
        self.perpendicular = self.projection - self.position + self.line_begin
        self.goal_path = self.line_end - self.position
        
        # Calculate distance to goal
        self.distance_to_goal = self.goal_path.length()
        self.distance_to_line = self.perpendicular.length()
        
        # Calculate angle between heading vector and target path vector
        self.angle_error = self.heading.angle(self.rabbit_path)

        # Rotate the heading vector according to the calculated angle and test correspondence
        # with the path vector. If not zero sign must be flipped. This is to avoid the sine trap.
        t1 = self.heading.rotate(self.angle_error)
        if self.rabbit_path.angle(t1) != 0 :
            self.angle_error = -self.angle_error
                
        self.goal_angle_error = self.heading.angle(self.goal_path)

        # Avoid the sine trap.
        t1 = self.heading.rotate(self.goal_angle_error)
        if self.goal_path.angle(t1) != 0 :
            self.goal_angle_error = -self.goal_angle_error
            
        # Determine zone
        if self.distance_to_line < self.z1_max_distance and math.fabs(self.angle_error) < self.z1_max_angle :
            self.zone_filter[self.z_ptr] = self.z1_value
        elif self.distance_to_line < self.z2_max_distance and math.fabs(self.angle_error) < self.z2_max_angle :
            self.zone_filter[self.z_ptr] = self.z2_value
        else :
            self.zone_filter[self.z_ptr] = self.z3_value
                
        self.z_ptr = self.z_ptr + 1
        if self.z_ptr >= self.z_filter_size :
            self.z_ptr = 0  
        
        self.zone = (sum(self.zone_filter)/len(self.zone_filter))
        if self.distance_to_goal < self.target_area :
            self.rabbit_factor = 1
            self.max_linear_velocity = self.z3_lin_vel
            self.max_angular_velocity = self.z3_ang_vel  
        elif self.zone < (self.z1_value + (self.z2_value/2) ) :
            self.corrected = True
            self.rabbit_factor = self.z1_rabbit
            self.max_linear_velocity = self.z1_lin_vel
            self.max_angular_velocity = self.z1_ang_vel
        elif self.zone < (self.z2_value + (self.z3_value/2) ) :
            self.rabbit_factor = self.z2_rabbit
            self.max_linear_velocity = self.z2_lin_vel
            self.max_angular_velocity = self.z2_ang_vel
        else :
            self.rabbit_factor = self.z3_rabbit
            self.max_linear_velocity = self.z3_lin_vel
            self.max_angular_velocity = self.z3_ang_vel
            
    def constructRabbit(self):      
           
        # Construct rabbit point
        self.rabbit = self.line - self.projection
        self.rabbit = self.rabbit.scale(self.rabbit_factor)
        self.rabbit += self.line_begin
        self.rabbit += self.projection
        
        self.rabbit_path = self.rabbit - Vector(self.position[0], self.position[1])
             
    def control_loop(self):
        """
            Method running the control loop. Distinguishes between target and goal to 
            adapt to other planners. For position planning the two will be the same.
        """
        self.sp_linear = self.max_linear_velocity
        self.sp_angular = self.angle_error
                       
        
        # Implement retarder to reduce linear velocity if angle error is too big
        if math.fabs(self.goal_angle_error) > self.max_angle_error :
            self.sp_linear *= self.retarder
       
        # Implement initial correction speed for large angle errors
        if not self.corrected :
            self.sp_linear *= self.retarder
            
        # Implement maximum linear velocity and maximum angular velocity
        if self.sp_linear > self.max_linear_velocity:
            self.sp_linear = self.max_linear_velocity
        if self.sp_linear < -self.max_linear_velocity:
            self.sp_linear = -self.max_linear_velocity
        if self.twist.twist.angular.z > self.max_angular_velocity:
            self.sp_angular = self.max_angular_velocity
        if self.sp_angular < -self.max_angular_velocity:
            self.sp_angular = -self.max_angular_velocity
        
        # Prohibit reverse driving
        if self.sp_linear < 0:
            self.sp_linear = 0
            
        # If not preempted, add a time stamp and publish the twist
        if not self.isPreemptRequested() :       
            self.twist = self.controller.generateTwist(self.sp_linear,self.sp_angular)           
            self.twist_pub.publish(self.twist)
        
    def onOdometry(self, msg):
        """
            Callback method for handling odometry messages
        """
        print("Oh noooooo!")
        # Extract the orientation quaternion
        self.quaternion[0] = msg.pose.pose.orientation.x
        self.quaternion[1] = msg.pose.pose.orientation.y
        self.quaternion[2] = msg.pose.pose.orientation.z
        self.quaternion[3] = msg.pose.pose.orientation.w
        (roll,pitch,self.yaw) = tf.transformations.euler_from_quaternion(self.quaternion)
        
        # Extract the position vector
        self.position[0] = msg.pose.pose.position.x
        self.position[1] = msg.pose.pose.position.y
    
    def empty_method(self):
        """
            Empty method pointer
        """
        return False
       
    def get_current_position(self):
        """
            Get current position from tf
        """
        if self.use_tf :     
            try:
                (position,head) = self.__listen.lookupTransform( self.odom_frame,self.base_frame,rospy.Time(0)) # The transform is returned as position (x,y,z) and an orientation quaternion (x,y,z,w).
                self.position[0] = position[0]
                self.position[1] = position[1]
                (roll,pitch,self.yaw) = tf.transformations.euler_from_quaternion(head)
            except (tf.LookupException, tf.ConnectivityException),err:
                rospy.loginfo("could not locate vehicle")             

            