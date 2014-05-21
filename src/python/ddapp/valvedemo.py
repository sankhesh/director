import os
import sys
import vtkAll as vtk
from ddapp import botpy
import math
import time
import types
import functools
import numpy as np

from ddapp import transformUtils
from ddapp import lcmUtils
from ddapp.timercallback import TimerCallback
from ddapp.asynctaskqueue import AsyncTaskQueue
from ddapp import objectmodel as om
from ddapp import visualization as vis
from ddapp import applogic as app
from ddapp.debugVis import DebugData
from ddapp import ikplanner
from ddapp import ioUtils
from ddapp.simpletimer import SimpleTimer
from ddapp.utime import getUtime
from ddapp import robotstate
from ddapp import robotplanlistener
from ddapp import segmentation
from ddapp import planplayback

import drc as lcmdrc

from PythonQt import QtCore, QtGui


class ValvePlannerDemo(object):

    def __init__(self, robotModel, footstepPlanner, manipPlanner, ikPlanner, handDriver, atlasDriver, multisenseDriver, affordanceFitFunction, sensorJointController, planPlaybackFunction, showPoseFunction):
        self.robotModel = robotModel
        self.footstepPlanner = footstepPlanner
        self.manipPlanner = manipPlanner
        self.ikPlanner = ikPlanner
        self.handDriver = handDriver
        self.atlasDriver = atlasDriver
        self.multisenseDriver = multisenseDriver
        self.affordanceFitFunction = affordanceFitFunction
        self.sensorJointController = sensorJointController
        self.planPlaybackFunction = planPlaybackFunction
        self.showPoseFunction = showPoseFunction
        self.graspingHand = 'left'

        self.planFromCurrentRobotState = True
        self.visOnly = True
        self.useFootstepPlanner = False

        # For autonomousExecute
        #self.visOnly = False
        #self.useFootstepPlanner = True

        self.userPromptEnabled = True
        self.walkingPlan = None
        self.preGraspPlan = None
        self.graspPlan = None
        self.constraintSet = None
        
        self.pointerTipTransformLocal = None
        self.pointerTipPath = []

        self.plans = []

        self.scribeInAir = False
        self.scribeDirection = 1 # 1 = clockwise | -1 = anticlockwise
        self.nextScribeAngle = -30 # suitable for both types of valve
        self.scribeRadius = None

        
    def addPlan(self, plan):
        self.plans.append(plan)

    def segmentValveWallAuto(self):
        om.removeFromObjectModel(om.findObjectByName('affordances'))
        vis.updatePolyData(segmentation.getCurrentRevolutionData(), 'pointcloud snapshot', parent='segmentation')
        self.affordanceFitFunction(.195)

    def setScribeAngleToCurrent(self):
        '''
        Compute the current angle of the robot's pointer relative to the valve
        '''

        for obj in om.getObjects():
            if obj.getProperty('Name') == 'pointer tip angle':
                om.removeFromObjectModel(obj)

        if (self.graspingHand == 'left'):
            tipFrame = self.robotModel.getLinkFrame('left_pointer_tip')
        else:
            tipFrame = self.robotModel.getLinkFrame('right_pointer_tip')
        #vis.updateFrame(tipFrame, 'pointer tip current', visible=True, scale=0.2)

        # Get the relative position of the pointer from the valve
        valveTransform =  transformUtils.copyFrame(self.valveFrame.transform)
        #print valveTransform.GetPosition()
        tipFrame.Concatenate(valveTransform.GetLinearInverse())
        #vis.updateFrame(tipFrame, 'point relative', visible=True, scale=0.1)

        # Set the Scribe angle to be the current angle
        tPosition = tipFrame.GetPosition()
        angle =  math.degrees(  math.atan2(tPosition[1], tPosition[0])  )
        radius = math.sqrt( tPosition[0]*tPosition[0] + tPosition[1]*tPosition[1] )
        print 'Current Scribe Angle: ', angle , ' and Radius: ' , radius
        self.nextScribeAngle = angle

        d = DebugData()
        d.addSphere(tPosition, radius=0.01)
        tPosition =[tPosition[0], tPosition[1], 0] # interested in the point on the plane too
        d.addSphere(tPosition, radius=0.01)

        currentTipMesh = d.getPolyData()
        self.currentTipPosition = vis.showPolyData(currentTipMesh, 'pointer tip angle', color=[1.0, 0.5, 0.0], cls=vis.AffordanceItem, parent=self.valveAffordance, alpha=0.5)
        self.currentTipPosition.actor.SetUserTransform(self.valveFrame.transform)


    def computeGroundFrame(self, robotModel):
        '''
        Given a robol model, returns a vtkTransform at a position between
        the feet, on the ground, with z-axis up and x-axis aligned with the
        robot pelvis x-axis.
        '''
        t1 = robotModel.getLinkFrame('l_foot')
        t2 = robotModel.getLinkFrame('r_foot')
        pelvisT = robotModel.getLinkFrame('pelvis')

        xaxis = [1.0, 0.0, 0.0]
        pelvisT.TransformVector(xaxis, xaxis)
        xaxis = np.array(xaxis)
        zaxis = np.array([0.0, 0.0, 1.0])
        yaxis = np.cross(zaxis, xaxis)
        yaxis /= np.linalg.norm(yaxis)
        xaxis = np.cross(yaxis, zaxis)

        stancePosition = (np.array(t2.GetPosition()) + np.array(t1.GetPosition())) / 2.0

        footHeight = 0.0811

        t = transformUtils.getTransformFromAxes(xaxis, yaxis, zaxis)
        t.PostMultiply()
        t.Translate(stancePosition)
        t.Translate([0.0, 0.0, -footHeight])

        return t


    def computeGraspFrame(self):

        assert self.valveAffordance

        # reach to center and back - for palm point
        position = [0.0, 0.0, -0.1]
        rpy = [90, 0, 180]

        t = transformUtils.frameFromPositionAndRPY(position, rpy)
        t.Concatenate(self.valveFrame.transform)

        self.frameSync = vis.FrameSync()
        om.removeFromObjectModel( self.valveAffordance.findChild('valve grasp frame') )
        self.graspFrame = vis.showFrame(t, 'valve grasp frame', parent=self.valveAffordance, visible=False, scale=0.2)
        self.frameSync.addFrame(self.graspFrame)
        self.frameSync.addFrame(self.valveFrame)


    def removePointerTipFrames(self):
        for obj in om.getObjects():
            if obj.getProperty('Name') == 'pointer tip frame desired':
                om.removeFromObjectModel(obj)

    def removePointerTipPath(self):
        for obj in om.getObjects():
            if obj.getProperty('Name') == 'pointer tip path':
                om.removeFromObjectModel(obj)

    def computePointerTipFrame(self, engagedTip):
        if engagedTip:
            tipDepth = 0.0
        else:
            tipDepth = -0.12 # - is outside the wheel, was 10

        assert self.valveAffordance

        position = [ self.scribeRadius*math.cos( math.radians( self.nextScribeAngle )) ,  self.scribeRadius*math.sin( math.radians( self.nextScribeAngle ))  , tipDepth]
        rpy = [90, 0, 180]

        t = transformUtils.frameFromPositionAndRPY(position, rpy)
        self.pointerTipTransformLocal = transformUtils.copyFrame(t)
        
        t.Concatenate(self.valveFrame.transform)
        self.pointerTipFrameDesired = vis.showFrame(t, 'pointer tip frame desired', parent=self.valveAffordance, visible=False, scale=0.2)

        
    def drawPointerTipPath(self):
      
        path = DebugData()
        for i in range(1,len(self.pointerTipPath)):
          p0 = self.pointerTipPath[i-1].GetPosition()
          p1 = self.pointerTipPath[i].GetPosition()
          path.addLine ( np.array( p0 ) , np.array(  p1 ), radius= 0.005)
          
        pathMesh = path.getPolyData()
        self.pointerTipLinePath = vis.showPolyData(pathMesh, 'pointer tip path', color=[0.0, 0.3, 1.0], cls=vis.AffordanceItem, parent=self.valveAffordance, alpha=0.6)
        self.pointerTipLinePath.actor.SetUserTransform(self.valveFrame.transform) 
        
    def computeStanceFrame(self):

        graspFrame = self.graspFrame.transform

        groundFrame = self.computeGroundFrame(self.robotModel)
        groundHeight = groundFrame.GetPosition()[2]

        graspPosition = np.array(graspFrame.GetPosition())
        graspYAxis = [0.0, 1.0, 0.0]
        graspZAxis = [0.0, 0.0, 1.0]
        graspFrame.TransformVector(graspYAxis, graspYAxis)
        graspFrame.TransformVector(graspZAxis, graspZAxis)

        xaxis = graspYAxis
        #xaxis = graspZAxis
        zaxis = [0, 0, 1]
        yaxis = np.cross(zaxis, xaxis)
        yaxis /= np.linalg.norm(yaxis)
        xaxis = np.cross(yaxis, zaxis)

        graspGroundFrame = transformUtils.getTransformFromAxes(xaxis, yaxis, zaxis)
        graspGroundFrame.PostMultiply()
        graspGroundFrame.Translate(graspPosition[0], graspPosition[1], groundHeight)


        if self.scribeInAir:
            position = [-0.6, -0.4, 0.0] # stand further away when scribing in air
        else:
            position = [-0.48, -0.4, 0.0]

        rpy = [0, 0, 16]

        # mirror stance frame for right hand:
        if (self.graspingHand == 'right'):
          position[1] = -position[1]
          rpy[2] = -rpy[2]
        
        t = transformUtils.frameFromPositionAndRPY(position, rpy)
        t.Concatenate(graspGroundFrame)

        om.removeFromObjectModel( self.valveAffordance.findChild('valve grasp stance') )
        self.graspStanceFrame = vis.showFrame(t, 'valve grasp stance', parent=self.valveAffordance, visible=False, scale=0.2)

        self.frameSync.addFrame(self.graspStanceFrame)


    def moveRobotToStanceFrame(self):
        frame = self.graspStanceFrame.transform

        self.sensorJointController.setPose('q_nom')
        stancePosition = frame.GetPosition()
        stanceOrientation = frame.GetOrientation()

        q = self.sensorJointController.q.copy()
        q[:2] = [stancePosition[0], stancePosition[1]]
        q[5] = math.radians(stanceOrientation[2])
        self.sensorJointController.setPose('EST_ROBOT_STATE', q)


    def computeFootstepPlan(self):
        startPose = self.getPlanningStartPose()
        goalFrame = self.graspStanceFrame.transform
        request = self.footstepPlanner.constructFootstepPlanRequest(startPose, goalFrame)
        self.footstepPlan = self.footstepPlanner.sendFootstepPlanRequest(request, waitForResponse=True)


    def computeWalkingPlan(self):
        startPose = self.getPlanningStartPose()
        self.walkingPlan = self.footstepPlanner.sendWalkingPlanRequest(self.footstepPlan, startPose, waitForResponse=True)
        self.addPlan(self.walkingPlan)


    def computePreGraspPlan(self):
        startPose = self.getPlanningStartPose()
        endPose = self.ikPlanner.getMergedPostureFromDatabase(startPose, 'General', 'arm up pregrasp', side=self.graspingHand)
        self.preGraspPlan = self.ikPlanner.computePostureGoal(startPose, endPose)
        self.addPlan(self.preGraspPlan)


    def computeGraspPlan(self):

        startPose = self.getPlanningStartPose()

        constraintSet = self.ikPlanner.planEndEffectorGoal(startPose, self.graspingHand, self.graspFrame, lockTorso=True)
        endPose, info = constraintSet.runIk()
        self.graspPlan = constraintSet.runIkTraj()

        self.addPlan(self.graspPlan)


    def initGazeConstraintSet(self, goalFrame):

        # create constraint set
        startPose = self.getPlanningStartPose()
        startPoseName = 'gaze_plan_start'
        endPoseName = 'gaze_plan_end'
        self.ikPlanner.addPose(startPose, startPoseName)
        self.ikPlanner.addPose(startPose, endPoseName)
        self.constraintSet = ikplanner.ConstraintSet(self.ikPlanner, [], startPoseName, endPoseName)
        self.constraintSet.endPose = startPose

        # add body constraints
        bodyConstraints = self.ikPlanner.createMovingBodyConstraints(startPoseName, lockBase=True, lockBack=False, lockLeftArm=self.graspingHand=='right', lockRightArm=self.graspingHand=='left')
        self.constraintSet.constraints.extend(bodyConstraints)

        # add gaze constraint
        self.graspToHandLinkFrame = self.ikPlanner.newGraspToHandFrame(self.graspingHand)
        gazeConstraint = self.ikPlanner.createGazeGraspConstraint(self.graspingHand, goalFrame, self.graspToHandLinkFrame)
        self.constraintSet.constraints.insert(0, gazeConstraint)


    def appendDistanceConstraint(self):

        # add point to point distance constraint
        c = ikplanner.ik.PointToPointDistanceConstraint()
        c.bodyNameA = self.ikPlanner.getHandLink(self.graspingHand)
        c.bodyNameB = 'world'
        c.pointInBodyA = self.graspToHandLinkFrame
        c.pointInBodyB = self.valveFrame.transform
        c.lowerBound = [self.scribeRadius]
        c.upperBound = [self.scribeRadius]
        self.constraintSet.constraints.insert(0, c)


    def appendGazeConstraintForTargetFrame(self, goalFrame, t):

        gazeConstraint = self.ikPlanner.createGazeGraspConstraint(self.graspingHand, goalFrame, self.graspToHandLinkFrame)
        gazeConstraint.tspan = [t, t]
        self.constraintSet.constraints.append(gazeConstraint)


    def appendPositionConstraintForTargetFrame(self, goalFrame, t):
        positionConstraint, _ = self.ikPlanner.createPositionOrientationGraspConstraints(self.graspingHand, goalFrame, self.graspToHandLinkFrame)
        positionConstraint.tspan = [t, t]
        self.constraintSet.constraints.append(positionConstraint)


    def planGazeTrajectory(self):

        self.ikPlanner.ikServer.usePointwise = False

        plan = self.constraintSet.runIkTraj()
        self.addPlan(plan)


    def commitFootstepPlan(self):
        self.footstepPlanner.commitFootstepPlan(self.footstepPlan)


    def commitManipPlan(self):
            self.manipPlanner.commitManipPlan(self.plans[-1])

    def sendNeckPitchLookDown(self):
        self.multisenseDriver.setNeckPitch(40)

    def sendNeckPitchLookForward(self):
        self.multisenseDriver.setNeckPitch(15)


    def waitForAtlasBehaviorAsync(self, behaviorName):
        assert behaviorName in self.atlasDriver.getBehaviorMap().values()
        while self.atlasDriver.getCurrentBehaviorName() != behaviorName:
            yield


    def printAsync(self, s):
        yield
        print s


    def userPrompt(self, message):

        if not self.userPromptEnabled:
            return

        yield
        result = raw_input(message)
        if result != 'y':
            raise Exception('user abort.')


    def delay(self, delayTimeInSeconds):
        yield
        t = SimpleTimer()
        while t.elapsed() < delayTimeInSeconds:
            yield


    def waitForCleanLidarSweepAsync(self):
        currentRevolution = self.multisenseDriver.displayedRevolution
        desiredRevolution = currentRevolution + 2
        while self.multisenseDriver.displayedRevolution < desiredRevolution:
            yield


    def spawnValveFrame(self, robotModel, height):

        position = [0.7, 0.22, height]
        rpy = [180, -90, -16]

        if (self.graspingHand == 'right'):
          position[1] = -position[1]
          rpy[2] = -rpy[2]

        t = transformUtils.frameFromPositionAndRPY(position, rpy)
        t.Concatenate(self.computeGroundFrame(robotModel))
        return t


    def spawnValveAffordance(self):
        spawn_height = 1.2192 # 4ft
        radius = 0.19558 # nominal initial value. 7.7in radius metal valve
        zwidth = 0.03

        valveFrame = self.spawnValveFrame(self.robotModel, spawn_height)

        folder = om.getOrCreateContainer('affordances')
        z = DebugData()
        z.addLine ( np.array([0, 0, -0.0254]) , np.array([0, 0, 0.0254]), radius=radius)
        valveMesh = z.getPolyData()

        self.valveAffordance = vis.showPolyData(valveMesh, 'valve', color=[0.0, 1.0, 0.0], cls=vis.FrameAffordanceItem, parent=folder, alpha=0.3)
        self.valveAffordance.actor.SetUserTransform(valveFrame)
        self.valveFrame = vis.showFrame(valveFrame, 'valve frame', parent=self.valveAffordance, visible=False, scale=0.2)

        params = dict(radius=radius, length=zwidth, xwidth=radius, ywidth=radius, zwidth=zwidth,
                      otdf_type='steering_cyl', friendly_name='valve')
        self.valveAffordance.setAffordanceParams(params)
        self.valveAffordance.updateParamsFromActorTransform()        

        
    def spawnValveLeverAffordance(self):
        spawn_height = 1.06 # 3.5ft
        pipe_radius = 0.01
        lever_length = 0.33

        valveFrame = self.spawnValveFrame(self.robotModel, spawn_height)
        folder = om.getOrCreateContainer('affordances')
        z = DebugData()
        z.addLine([0,0,0], [ lever_length , 0, 0], radius=pipe_radius)
        valveMesh = z.getPolyData()        
        
        self.valveAffordance = vis.showPolyData(valveMesh, 'valve lever', color=[0.0, 1.0, 0.0], cls=vis.FrameAffordanceItem, parent=folder, alpha=0.3)
        self.valveAffordance.actor.SetUserTransform(valveFrame)
        self.valveFrame = vis.showFrame(valveFrame, 'lever frame', parent=self.valveAffordance, visible=False, scale=0.2)
        
        otdfType = 'lever_valve'
        params = dict( radius=pipe_radius, length=lever_length, friendly_name=otdfType, otdf_type=otdfType)
        self.valveAffordance.setAffordanceParams(params)
        self.valveAffordance.updateParamsFromActorTransform()

        
    def findValveAffordance(self):
        self.nextScribeAngle = -30 # reset angle when a new valve is found
      
        self.valveAffordance = om.findObjectByName('valve')
        self.valveFrame = om.findObjectByName('valve frame')

        self.scribeRadius = self.valveAffordance.params.get('radius') - 0.06

        self.graspingHand = 'left'
        self.computeGraspFrame()
        self.computeStanceFrame()


    def findValveLeverAffordance(self):
        self.nextScribeAngle = -30 # reset angle when a new valve is found

        self.valveAffordance = om.findObjectByName('valve lever')
        self.valveFrame = om.findObjectByName('lever frame')

        # length of lever is equivalent to radius of valve
        self.scribeRadius = self.valveAffordance.params.get('length') - 0.07

        self.graspingHand = 'right'
        self.computeGraspFrame()
        self.computeStanceFrame()


    def getEstimatedRobotStatePose(self):
        return np.array(self.sensorJointController.getPose('EST_ROBOT_STATE'))


    def getPlanningStartPose(self):
        if self.planFromCurrentRobotState:
            return self.getEstimatedRobotStatePose()
        else:
            if self.plans:
                return robotstate.convertStateMessageToDrakePose(self.plans[-1].plan[-1])
            else:
                return self.getEstimatedRobotStatePose()


    def removeFootstepPlan(self):
        om.removeFromObjectModel(om.findObjectByName('footstep plan'))
        self.footstepPlan = None


    def playNominalPlan(self):
        assert None not in self.plans
        self.planPlaybackFunction(self.plans)


    def computePreGraspPlanGaze(self):

        self.computePointerTipFrame(0)
        self.initGazeConstraintSet(self.pointerTipFrameDesired)
        self.appendPositionConstraintForTargetFrame(self.pointerTipFrameDesired, 1)
        self.planGazeTrajectory()


    def computeInsertPlan(self):
        self.computePointerTipFrame(1)
        self.initGazeConstraintSet(self.pointerTipFrameDesired)
        self.appendPositionConstraintForTargetFrame(self.pointerTipFrameDesired, 1)
        self.planGazeTrajectory()


    def computeTurnPlan(self, turnDegrees=360, numberOfSamples=12):
        self.pointerTipPath = []
        self.removePointerTipFrames()
        self.removePointerTipPath()

        degreeStep = float(turnDegrees) / numberOfSamples
        tipMode = 0 if self.scribeInAir else 1

        self.computePointerTipFrame(tipMode)
        self.initGazeConstraintSet(self.pointerTipFrameDesired)
        #self.appendDistanceConstraint()
        self.pointerTipPath.append(self.pointerTipTransformLocal)

        for i in xrange(numberOfSamples):
            self.nextScribeAngle += self.scribeDirection*degreeStep
            self.computePointerTipFrame(tipMode)
            self.appendPositionConstraintForTargetFrame(self.pointerTipFrameDesired, i+1)
            self.pointerTipPath.append(self.pointerTipTransformLocal)

        gazeConstraint = self.constraintSet.constraints[0]
        assert isinstance(gazeConstraint, ikplanner.ik.WorldGazeDirConstraint)
        gazeConstraint.tspan = [1.0, numberOfSamples]

        self.drawPointerTipPath()
        self.ikPlanner.ikServer.maxDegreesPerSecond = 10
        self.planGazeTrajectory()
        self.ikPlanner.ikServer.maxDegreesPerSecond = 30


    def computeStandPlan(self):
        startPose = self.getPlanningStartPose()
        self.standPlan = self.ikPlanner.computeNominalPlan(startPose)
        self.addPlan(self.standPlan)


    def computeNominalPlan(self, mode='valve'):

        self.removeFootstepPlan()
        self.removePointerTipFrames()
        self.removePointerTipPath()

        if (mode=='valve'):
            self.graspingHand='left'
            self.findValveAffordance()
            turn_angle = 360
        else:
            self.graspingHand='right'
            self.findValveLeverAffordance()
            turn_angle = 90

        self.plans = []

        if self.useFootstepPlanner:
            self.computeFootstepPlan()
            self.computeWalkingPlan()
        else:
            self.moveRobotToStanceFrame()

        self.computePreGraspPlan()
        self.computePreGraspPlanGaze()

        self.computeInsertPlan()

        self.computeTurnPlan(turn_angle)
        self.computePreGraspPlanGaze()
        self.computePreGraspPlan()
        self.computeStandPlan()

        self.playNominalPlan()


    def computeNominalPlanBoth(self):

        self.removeFootstepPlan()
        self.removePointerTipFrames()
        self.removePointerTipPath()

        self.graspingHand='right'
        self.findValveLeverAffordance()
        turn_angle = 90

        self.plans = []

        if self.useFootstepPlanner:
            self.computeFootstepPlan()
            self.computeWalkingPlan()
        else:
            self.moveRobotToStanceFrame()

        self.computePreGraspPlan()
        self.computePreGraspPlanGaze()

        self.computeInsertPlan()

        self.computeTurnPlan(turn_angle)
        self.computePreGraspPlanGaze()
        self.computePreGraspPlan()
        self.computeStandPlan()

        # plan valve affordance:
        self.graspingHand='left'
        self.findValveAffordance()
        turn_angle = 360

        #self.plans = []

        if self.useFootstepPlanner:
            self.computeFootstepPlan()
            self.computeWalkingPlan()
        else:
            self.moveRobotToStanceFrame()

        self.computePreGraspPlan()
        self.computePreGraspPlanGaze()

        self.computeInsertPlan()

        self.computeTurnPlan(turn_angle)
        self.computePreGraspPlanGaze()
        self.computePreGraspPlan()
        self.computeStandPlan()

        self.playNominalPlan()


    def waitForPlanExecution(self, plan):
        planElapsedTime = planplayback.PlanPlayback.getPlanElapsedTime(plan)
        print 'waiting for plan execution:', planElapsedTime

        return self.delay(planElapsedTime + 1.0)


    def animateLastPlan(self):
        plan = self.plans[-1]

        if not self.visOnly:
            self.commitManipPlan()

        return self.waitForPlanExecution(plan)


    def addWalkingTasksToQueue(self, taskQueue, planFunc, walkFunc):

        if self.useFootstepPlanner:
            taskQueue.addTask(planFunc)

            if self.visOnly:
                taskQueue.addTask(self.computeWalkingPlan)
                taskQueue.addTask(self.animateLastPlan)
            else:

                taskQueue.addTask(self.userPrompt('send stand command. continue? y/n: '))
                taskQueue.addTask(self.atlasDriver.sendStandCommand)
                taskQueue.addTask(self.waitForAtlasBehaviorAsync('stand'))

                taskQueue.addTask(self.userPrompt('commit footsteps. continue? y/n: '))
                taskQueue.addTask(self.commitFootstepPlan)
                taskQueue.addTask(self.waitForAtlasBehaviorAsync('step'))
                taskQueue.addTask(self.waitForAtlasBehaviorAsync('stand'))

            taskQueue.addTask(self.removeFootstepPlan)
        else:
            taskQueue.addTask(walkFunc)



    def autonomousExecute(self):


        taskQueue = AsyncTaskQueue()
        taskQueue.addTask(self.removePointerTipFrames)
        taskQueue.addTask(self.removePointerTipPath)
        
        taskQueue.addTask(self.segmentValveWallAuto)
        taskQueue.addTask(self.userPrompt('Accept valve fit, continue? y/n: '))
        taskQueue.addTask(self.findValveAffordance)


        taskQueue.addTask(self.sendNeckPitchLookForward)
        self.addWalkingTasksToQueue(taskQueue, self.computeFootstepPlan, self.moveRobotToStanceFrame)
        taskQueue.addTask(self.atlasDriver.sendManipCommand)
        taskQueue.addTask(self.waitForAtlasBehaviorAsync('manip'))


        taskQueue.addTask(self.waitForCleanLidarSweepAsync)
        taskQueue.addTask(self.segmentValveWallAuto)
        taskQueue.addTask(self.userPrompt('Accept valve re-fit, continue? y/n: '))
        taskQueue.addTask(self.findValveAffordance)


        planningFunctions = [
                    self.computePreGraspPlan,
                    self.computePreGraspPlanGaze,
                    self.computeInsertPlan,
                    self.computeTurnPlan,
                    self.computePreGraspPlanGaze,
                    self.computePreGraspPlan,
                    self.computeStandPlan,
                    ]


        for planFunc in planningFunctions:
            taskQueue.addTask(planFunc)
            taskQueue.addTask(self.userPrompt('c continue? y/n: '))
            taskQueue.addTask(self.animateLastPlan)


        ############################################################################
        ############################################################################
        taskQueue.addTask(self.segmentValveWallAuto)
        taskQueue.addTask(self.userPrompt('Accept lever fit, continue? y/n: '))
        taskQueue.addTask(self.findValveLeverAffordance)


        taskQueue.addTask(self.sendNeckPitchLookForward)
        self.addWalkingTasksToQueue(taskQueue, self.computeFootstepPlan, self.moveRobotToStanceFrame)
        taskQueue.addTask(self.atlasDriver.sendManipCommand)
        taskQueue.addTask(self.waitForAtlasBehaviorAsync('manip'))


        taskQueue.addTask(self.waitForCleanLidarSweepAsync)
        taskQueue.addTask(self.segmentValveWallAuto)
        taskQueue.addTask(self.userPrompt('Accept lever re-fit, continue? y/n: '))
        taskQueue.addTask(self.findValveLeverAffordance)


        planningFunctions = [
                    self.computePreGraspPlan,
                    self.computePreGraspPlanGaze,
                    self.computeInsertPlan,
                    self.computeTurnPlan,
                    self.computePreGraspPlanGaze,
                    self.computePreGraspPlan,
                    self.computeStandPlan,
                    ]


        for planFunc in planningFunctions:
            taskQueue.addTask(planFunc)
            taskQueue.addTask(self.userPrompt('f continue? y/n: '))
            taskQueue.addTask(self.animateLastPlan)


        taskQueue.addTask(self.printAsync('done!'))

        return taskQueue


