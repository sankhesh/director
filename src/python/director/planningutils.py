'''
PlanningUtils is a centralised helper to provide functions commonly used by 
motion planners
'''
import numpy as np
from director import robotstate

class PlanningUtils(object):
    def __init__(self, robotStateModel, robotStateJointController):
        self.robotStateModel = robotStateModel
        self.robotStateJointController = robotStateJointController

        # Get joint limits
        self.jointLimitsLower = np.array([self.robotStateModel.model.getJointLimits(jointName)[0] for jointName in robotstate.getDrakePoseJointNames()])
        self.jointLimitsUpper = np.array([self.robotStateModel.model.getJointLimits(jointName)[1] for jointName in robotstate.getDrakePoseJointNames()])

    def getPlanningStartPose(self, clampToJointLimits=True):
        startPose = np.array(self.robotStateJointController.q)

        if clampToJointLimits:
            startPose = np.clip(startPose, self.jointLimitsLower, self.jointLimitsUpper)
        
        return startPose
